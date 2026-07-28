"""Fail-closed Git access for the separate Grim Dawn save vault.

This module intentionally owns every Git subprocess used by save sync.  It
never invokes a shell and it only mutates the three paths owned by the vault.
Locking and the CLI workflow deliberately live in later tickets.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import os
import re
import stat
import unicodedata
import uuid
from typing import Iterable

from grim_dawn_sync.errors import EXIT_CONFLICT, EXIT_CONFIGURATION, EXIT_RECOVERY_REQUIRED, EXIT_VALIDATION, SyncError
from grim_dawn_sync.manifest import assert_safe_save_file, stable_manifest, validate_manifest_path
from grim_dawn_sync.snapshot import _copy_verified
from grim_dawn_sync.validation import validate_players


_MANAGED = ("save", ".sync/manifest.json", ".sync/vault.json")


def _reparse(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _safe_lstat(path: Path) -> os.stat_result:
    """Inspect a path without ever resolving a link/junction."""
    try:
        value = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as error:
        raise SyncError("unsafe_vault_tree", "Vault path could not be safely inspected.", EXIT_RECOVERY_REQUIRED) from error
    if stat.S_ISLNK(value.st_mode) or _reparse(value):
        raise SyncError("unsafe_vault_tree", "Vault paths may not be links or reparse points.", EXIT_RECOVERY_REQUIRED)
    return value


def _remove_safe_tree(root: Path) -> None:
    """Remove only a verified ordinary tree; never follow a reparse point."""
    root_stat = _safe_lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise SyncError("unsafe_vault_tree", "Rollback artifact is not a directory.", EXIT_RECOVERY_REQUIRED)
    def walk(directory: Path) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            raise SyncError("snapshot_cleanup_incomplete", "Rollback artifact could not be scanned.", EXIT_RECOVERY_REQUIRED) from error
        for entry in entries:
            child = Path(entry.path); child_stat = _safe_lstat(child)
            if stat.S_ISDIR(child_stat.st_mode):
                walk(child)
                try: os.rmdir(child)
                except OSError as error: raise SyncError("snapshot_cleanup_incomplete", "Rollback artifact could not be removed.", EXIT_RECOVERY_REQUIRED) from error
            elif stat.S_ISREG(child_stat.st_mode):
                try: os.unlink(child)
                except OSError as error: raise SyncError("snapshot_cleanup_incomplete", "Rollback artifact could not be removed.", EXIT_RECOVERY_REQUIRED) from error
            else:
                raise SyncError("unsafe_vault_tree", "Rollback artifact has an unsafe entry.", EXIT_RECOVERY_REQUIRED)
    walk(root)
    try: os.rmdir(root)
    except OSError as error: raise SyncError("snapshot_cleanup_incomplete", "Rollback artifact could not be removed.", EXIT_RECOVERY_REQUIRED) from error


def _safe_destination_ancestors(destination: Path) -> None:
    """Reject a destination that exists, dangles, or sits below a link."""
    current = destination
    while True:
        try:
            _safe_lstat(current)
            if current == destination:
                raise SyncError("restore_destination_exists", "Restore destination must not exist.", EXIT_VALIDATION)
        except FileNotFoundError:
            pass
        except SyncError as error:
            if error.code == "unsafe_vault_tree":
                raise SyncError("unsafe_vault_tree", "Restore destination has an unsafe ancestor.", EXIT_VALIDATION) from error
            raise
        if current.parent == current: break
        current = current.parent


def _canonical_tar_members(members: list[object]) -> tuple[list[tuple[object, str]], set[str]]:
    """Validate the complete archive before creating any destination path."""
    entries: list[tuple[object, str]] = []; kinds: dict[str, str] = {}; folded: set[str] = set()
    for member in members:
        name = member.name
        if not isinstance(name, str) or "\0" in name or "\\" in name or name.startswith("/") or name.startswith("\\"):
            raise SyncError("unsafe_vault_tree", "Vault archive contains an unsafe entry.", EXIT_VALIDATION)
        if not (member.isfile() or member.isdir()) or member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise SyncError("unsafe_vault_tree", "Vault archive contains an unsafe entry.", EXIT_VALIDATION)
        trimmed = name.rstrip("/")
        if trimmed == "save":
            if not member.isdir(): raise SyncError("unsafe_vault_tree", "Vault archive save root must be a directory.", EXIT_VALIDATION)
            continue
        if not trimmed.startswith("save/"):
            raise SyncError("unsafe_vault_tree", "Vault archive escapes its save root.", EXIT_VALIDATION)
        try: relative = validate_manifest_path(trimmed[5:])
        except SyncError as error: raise SyncError("unsafe_vault_tree", "Vault archive contains an unsafe path.", EXIT_VALIDATION) from error
        key = unicodedata.normalize("NFC", relative).casefold()
        if key in folded or relative in kinds:
            raise SyncError("unsafe_vault_tree", "Vault archive has colliding paths.", EXIT_VALIDATION)
        folded.add(key); kinds[relative] = "dir" if member.isdir() else "file"; entries.append((member, relative))
    for path, kind in kinds.items():
        parts = path.split("/")
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            if kinds.get(ancestor) != "dir":
                raise SyncError("unsafe_vault_tree", "Vault archive has file-directory conflicts.", EXIT_VALIDATION)
    return entries, {path for member, path in entries if member.isfile()}


def _remote_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value)
        or value.endswith(".")
        or ".." in value
    ):
        raise SyncError("invalid_git_identifier", "Invalid Git remote.", EXIT_CONFIGURATION)
    return value


def _branch_name(value: str) -> str:
    forbidden = " ~^:?*[\\"
    if (
        not isinstance(value, str)
        or not value
        or value == "@"
        or value.startswith(("/", "."))
        or value.endswith(("/", "."))
        or "//" in value
        or ".." in value
        or "@{" in value
        or any(ord(char) < 32 or ord(char) == 127 or char in forbidden for char in value)
    ):
        raise SyncError("invalid_git_identifier", "Invalid Git branch.", EXIT_CONFIGURATION)
    components = value.split("/")
    if any(not component or component.startswith(".") or component.endswith(".lock") for component in components):
        raise SyncError("invalid_git_identifier", "Invalid Git branch.", EXIT_CONFIGURATION)
    return value


_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _oid_value(value: str, label: str = "commit") -> str:
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise SyncError("invalid_git_identifier", f"Invalid Git {label}.", EXIT_CONFIGURATION)
    return value


def _snapshot_token(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or any(char in value for char in "/\\\r\n\0") or ".." in value:
        raise SyncError("invalid_snapshot_token", f"Invalid snapshot {label}.", EXIT_CONFIGURATION)
    return value


@dataclass(frozen=True)
class GitResult:
    args: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int


class GitRunner:
    """Small argv-only adapter; tests may inspect ``commands``."""
    def __init__(self, cwd: Path, executable: str = "git") -> None:
        self.cwd = Path(cwd).resolve(); self.executable = executable; self.commands: list[tuple[str, ...]] = []

    def run(self, *args: str, check: bool = True, input_text: str | None = None) -> GitResult:
        argv = (self.executable, *args); self.commands.append(argv)
        try:
            completed = subprocess.run(list(argv), cwd=self.cwd, shell=False, input=input_text, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
        except OSError as error:
            raise SyncError("git_unavailable", "Git could not be executed.", EXIT_CONFIGURATION) from error
        result = GitResult(tuple(args), completed.stdout, completed.stderr, completed.returncode)
        if check and result.returncode:
            raise SyncError("git_command_failed", "Git command failed.", EXIT_CONFLICT, {"command": args[0] if args else "git"})
        return result

    def run_bytes(self, *args: str) -> bytes:
        argv = (self.executable, *args); self.commands.append(argv)
        try:
            completed = subprocess.run(list(argv), cwd=self.cwd, shell=False, capture_output=True, check=False)
        except OSError as error:
            raise SyncError("git_unavailable", "Git could not be executed.", EXIT_CONFIGURATION) from error
        if completed.returncode:
            raise SyncError("git_command_failed", "Git command failed.", EXIT_CONFLICT, {"command": args[0] if args else "git"})
        return completed.stdout


@dataclass(frozen=True)
class VaultStatus:
    relation: str
    local_oid: str | None
    remote_oid: str | None


class GitVault:
    def __init__(self, repo: Path, *, remote: str = "origin", branch: str = "main", runner: GitRunner | None = None) -> None:
        self.repo = Path(repo).resolve(); self.remote = _remote_name(remote); self.branch = _branch_name(branch)
        self.runner = runner or GitRunner(self.repo)

    @property
    def remote_ref(self) -> str: return f"refs/remotes/{self.remote}/{self.branch}"
    @property
    def remote_head(self) -> str: return f"refs/heads/{self.branch}"

    def preflight(self) -> None:
        if not self.repo.is_dir() or not (self.repo / ".git").exists():
            raise SyncError("vault_not_clone", "Vault must be a Git clone.", EXIT_CONFIGURATION)
        hooks = self.runner.run("config", "--get", "core.hooksPath", check=False).stdout.strip()
        default_hooks = self.repo / ".git" / "hooks"
        controlled_hooks = self.repo / ".sync" / "empty-hooks"
        allowed_hooks = hooks == ".sync/empty-hooks" and controlled_hooks.is_dir() and not any(controlled_hooks.iterdir())
        if not allowed_hooks and (hooks or (default_hooks.is_dir() and any(default_hooks.iterdir()))):
            raise SyncError("vault_hooks_present", "Vault Git hooks are not permitted.", EXIT_CONFLICT)
        tracked = self.runner.run_bytes("ls-files", "-z").split(b"\0")
        allowed = set(_MANAGED)
        if any(item and item.decode("utf-8", "surrogateescape") not in allowed and not item.decode("utf-8", "surrogateescape").startswith("save/") for item in tracked):
            raise SyncError("vault_unmanaged_tracked", "Vault contains unmanaged tracked paths.", EXIT_CONFLICT)
        if self.runner.run("status", "--porcelain=v1", "--untracked-files=all").stdout:
            raise SyncError("vault_dirty", "Vault has unmanaged or pending changes.", EXIT_CONFLICT)

    def remote_oid(self) -> str | None:
        result = self.runner.run("ls-remote", "--refs", self.remote, self.remote_head)
        raw_rows = [row for row in result.stdout.splitlines() if row]
        rows = [row.split("\t", 1) for row in raw_rows]
        if len(rows) > 1 or (rows and (len(rows[0]) != 2 or rows[0][1] != self.remote_head or not _OID.fullmatch(rows[0][0]))):
            raise SyncError("malformed_remote_ref", "Remote main ref was malformed.", EXIT_CONFLICT)
        return rows[0][0] if rows else None

    def fetch(self) -> None:
        self.runner.run("fetch", "--no-tags", self.remote, f"{self.remote_head}:{self.remote_ref}")

    def _oid(self, ref: str) -> str | None:
        result = self.runner.run("rev-parse", "--verify", "--quiet", ref, check=False)
        return result.stdout.strip() or None

    def reconcile(self) -> VaultStatus:
        local, remote = self._oid("HEAD"), self._oid(self.remote_ref)
        if local is None: return VaultStatus("unborn", None, remote)
        if remote is None: return VaultStatus("ahead", local, None)
        if local == remote: return VaultStatus("equal", local, remote)
        left = self.runner.run("merge-base", "--is-ancestor", "HEAD", self.remote_ref, check=False).returncode
        right = self.runner.run("merge-base", "--is-ancestor", self.remote_ref, "HEAD", check=False).returncode
        if left > 1 or right > 1: raise SyncError("git_command_failed", "Git ancestry check failed.", EXIT_CONFLICT)
        local_ancestor, remote_ancestor = left == 0, right == 0
        if local_ancestor: return VaultStatus("behind", local, remote)
        if remote_ancestor: return VaultStatus("ahead", local, remote)
        return VaultStatus("diverged", local, remote)

    def update_fast_forward(self) -> VaultStatus:
        self.preflight(); self.fetch(); status = self.reconcile()
        if status.relation == "behind": self.runner.run("merge", "--ff-only", self.remote_ref)
        elif status.relation == "diverged": raise SyncError("vault_diverged", "Vault history diverged; automatic merge is forbidden.", EXIT_CONFLICT, {"local": status.local_oid, "remote": status.remote_oid})
        return status

    def snapshot(self, source: Path, *, machine_id: str, session_id: str, retries: int = 1, window_seconds: float = 0, validator=validate_players) -> str:
        self.preflight()
        machine_id = _snapshot_token(machine_id, "machine ID"); session_id = _snapshot_token(session_id, "session ID")
        expected = stable_manifest(source, machine_id=machine_id, retries=retries, window_seconds=window_seconds)
        validation = validator(source, expected)
        if not validation.get("ok"):
            raise SyncError(validation["classification"], "Save validation did not permit snapshot.", EXIT_VALIDATION)
        target = self.repo / "save"; stage = self.repo / ".sync" / f".save-stage-{session_id}"; rollback = self.repo / ".sync" / f".save-rollback-{session_id}"
        if stage.exists() or stage.is_symlink() or rollback.exists() or rollback.is_symlink(): raise SyncError("snapshot_name_collision", "Vault snapshot artifacts already exist.", EXIT_RECOVERY_REQUIRED)
        try: stage.parent.mkdir(exist_ok=True)
        except OSError as error: raise SyncError("snapshot_metadata_failed", "Vault snapshot metadata directory could not be prepared.", EXIT_RECOVERY_REQUIRED) from error
        _copy_verified(source, stage, expected, machine_id, retries, window_seconds, validator)
        parked = False
        try:
            if target.exists() or target.is_symlink(): os.rename(target, rollback); parked = True
            os.rename(stage, target)
        except OSError as error:
            if parked:
                try: os.rename(rollback, target)
                except OSError as rollback_error: raise SyncError("snapshot_recovery_required", "Vault snapshot rollback failed.", EXIT_RECOVERY_REQUIRED) from rollback_error
            raise SyncError("snapshot_swap_failed", "Vault snapshot swap failed.", EXIT_RECOVERY_REQUIRED) from error
        try:
            sync = self.repo / ".sync"; sync.mkdir(exist_ok=True)
            for name, payload in (("manifest.json", expected), ("vault.json", {"schema_version": "1.0.0", "machine_id": machine_id, "session_id": session_id, "root_hash": expected["root_hash"]})):
                temp = sync / f".{name}.{session_id}.tmp"; temp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"); os.replace(temp, sync / name)
        except OSError as error:
            raise SyncError("snapshot_metadata_failed", "Vault metadata write failed; rollback artifact was retained.", EXIT_RECOVERY_REQUIRED) from error
        self.runner.run("add", "--", *_MANAGED)
        staged = self.runner.run("diff", "--cached", "--quiet", check=False).returncode
        if staged not in (0, 1): raise SyncError("git_commit_failed", "Could not inspect vault staged changes.", EXIT_RECOVERY_REQUIRED)
        if staged == 1:
            message = f"Save session from {machine_id} root={expected['root_hash']} session={session_id}"
            if self.runner.run("commit", "-m", message, check=False).returncode:
                raise SyncError("git_commit_failed", "Vault snapshot commit failed; rollback artifact was retained.", EXIT_RECOVERY_REQUIRED)
        oid = self._oid("HEAD")
        if oid is None: raise SyncError("git_commit_failed", "Vault did not produce a commit.", EXIT_RECOVERY_REQUIRED)
        if rollback.exists():
            try:
                _remove_safe_tree(rollback)
            except (OSError, SyncError) as error:
                raise SyncError("snapshot_cleanup_incomplete", "Vault commit succeeded but rollback artifact was retained.", EXIT_RECOVERY_REQUIRED) from error
        return oid

    def push(self, expected_oid: str | None = None) -> str:
        oid = self._oid("HEAD")
        if oid is None: raise SyncError("vault_unborn", "Vault has no local commit to push.", EXIT_CONFIGURATION)
        if expected_oid and oid != expected_oid: raise SyncError("vault_head_changed", "Vault HEAD changed before push.", EXIT_CONFLICT)
        pushed = self.runner.run("push", self.remote, f"HEAD:{self.remote_head}", check=False)
        if pushed.returncode:
            raise SyncError("push_incomplete", "Local vault commit was retained; remote push did not complete.", EXIT_RECOVERY_REQUIRED, {"local_commit": oid})
        confirmed = self.remote_oid()
        if confirmed != oid: raise SyncError("push_incomplete", "Local vault commit was retained; remote push was not confirmed.", EXIT_RECOVERY_REQUIRED, {"local_commit": oid})
        return oid

    def extract_save(self, commit: str, destination: Path, *, machine_id: str, retries: int = 1, window_seconds: float = 0, validator=validate_players) -> Path:
        commit = _oid_value(commit)
        if self.runner.run("cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode:
            raise SyncError("invalid_restore_commit", "Restore commit was not available.", EXIT_VALIDATION)
        ancestry = self.runner.run("merge-base", "--is-ancestor", commit, "HEAD", check=False).returncode
        if ancestry == 1: raise SyncError("restore_commit_not_in_history", "Restore commit is not in vault history.", EXIT_VALIDATION)
        if ancestry != 0: raise SyncError("git_command_failed", "Restore commit ancestry could not be verified.", EXIT_CONFLICT)
        _safe_destination_ancestors(destination)
        listing = self.runner.run("ls-tree", "-r", "-z", commit, "--", "save").stdout
        tree_files: set[str] = set()
        for item in listing.split("\0"):
            if not item: continue
            if "\t" not in item: raise SyncError("unsafe_vault_tree", "Vault tree entry was malformed.", EXIT_VALIDATION)
            meta, path = item.split("\t", 1); fields = meta.split()
            if len(fields) != 3 or fields[0] != "100644" or fields[1] != "blob" or not _OID.fullmatch(fields[2]) or not path.startswith("save/"):
                raise SyncError("unsafe_vault_tree", "Vault tree contains a non-file save entry.", EXIT_VALIDATION)
            try: relative = validate_manifest_path(path[5:])
            except SyncError as error: raise SyncError("unsafe_vault_tree", "Vault tree contains an unsafe path.", EXIT_VALIDATION) from error
            key = unicodedata.normalize("NFC", relative).casefold()
            if key in {unicodedata.normalize("NFC", value).casefold() for value in tree_files}:
                raise SyncError("unsafe_vault_tree", "Vault tree has colliding paths.", EXIT_VALIDATION)
            tree_files.add(relative)
        try:
            payload = self.runner.run_bytes("archive", "--format=tar", commit, "save")
        except SyncError as error:
            raise SyncError("git_archive_failed", "Could not read historical save.", EXIT_VALIDATION) from error
        import tarfile, io
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as tar:
            entries, archive_files = _canonical_tar_members(tar.getmembers())
            if archive_files != tree_files:
                raise SyncError("unsafe_vault_tree", "Vault archive does not match its Git tree.", EXIT_VALIDATION)
            stage = destination.with_name(f".save-sync-extract-stage-{uuid.uuid4().hex}")
            _safe_destination_ancestors(stage)
            try:
                stage.mkdir()
            except OSError as error:
                raise SyncError("historical_extract_failed", "Historical save staging could not be created.", EXIT_VALIDATION) from error
            for member, relative in sorted(entries, key=lambda item: (len(item[1].split("/")), item[1])):
                output = stage / Path(*PurePosixPath(relative).parts)
                if member.isdir():
                    try: output.mkdir(exist_ok=False)
                    except OSError as error: raise SyncError("historical_extract_failed", "Historical save extraction failed; staging was retained.", EXIT_VALIDATION) from error
            for member, relative in entries:
                if member.isdir(): continue
                output = stage / Path(*PurePosixPath(relative).parts)
                # Parents came from the validated canonical table; inspect again for races.
                for parent in (stage, *[stage.joinpath(*PurePosixPath(relative).parts[:i]) for i in range(1, len(PurePosixPath(relative).parts))]): _safe_lstat(parent)
                try:
                    with tar.extractfile(member) as src:
                        if src is None: raise SyncError("unsafe_vault_tree", "Vault archive file was unavailable.", EXIT_VALIDATION)
                        with output.open("xb") as dst: shutil.copyfileobj(src, dst)
                except OSError as error:
                    raise SyncError("historical_extract_failed", "Historical save extraction failed; staging was retained.", EXIT_VALIDATION) from error
                _safe_lstat(output)
        try:
            manifest = stable_manifest(stage, machine_id=machine_id, retries=retries, window_seconds=window_seconds)
            validation = validator(stage, manifest)
        except OSError as error:
            raise SyncError("historical_extract_validation_failed", "Historical save validation failed; staging was retained.", EXIT_VALIDATION) from error
        if not validation.get("ok"):
            raise SyncError(validation["classification"], "Historical save validation failed.", EXIT_VALIDATION)
        _safe_destination_ancestors(destination)
        _safe_lstat(stage)
        try: os.rename(stage, destination)
        except OSError as error: raise SyncError("historical_extract_publish_failed", "Historical save could not be published; staging was retained.", EXIT_VALIDATION) from error
        try: _safe_lstat(destination)
        except SyncError as error: raise SyncError("unsafe_vault_tree", "Published historical save was unsafe.", EXIT_VALIDATION) from error
        return destination
