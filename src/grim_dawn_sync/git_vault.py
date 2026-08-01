"""Fail-closed Git access for the separate Grim Dawn save vault.

This module intentionally owns every Git subprocess used by save sync.  It
never invokes a shell and it only mutates the three paths owned by the vault.
Locking and the CLI workflow deliberately live in later tickets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import time
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import os
import re
import stat
import unicodedata
import uuid
import math
from typing import Callable, Iterable

from grim_dawn_sync.errors import EXIT_CONFLICT, EXIT_CONFIGURATION, EXIT_RECOVERY_REQUIRED, EXIT_VALIDATION, SyncError
from grim_dawn_sync.catalog_capability import read_remote_identity
from grim_dawn_sync.manifest import (
    MANIFEST_SCHEMA_VERSION,
    assert_safe_save_file,
    is_character_player_path,
    stable_manifest,
    validate_manifest_path,
)
from grim_dawn_sync.snapshot import _copy_verified
from grim_dawn_sync.validation import validate_players


_MANAGED = ("save", ".sync/manifest.json", ".sync/vault.json")
_DEFAULT_GIT_TIMEOUT_SECONDS = 60.0
_TAG_FETCH_BATCH_SIZE = 16
_TAG_REF_OUTPUT_LIMIT = 128 * 1024
_TAG_REF_COUNT_LIMIT = 100
_TAG_REF_BYTE_LIMIT = 512
_CAT_FILE_BATCH_SIZE = 256


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


def _safe_ordinary_directory(path: Path) -> bool:
    """Return whether ``path`` is an ordinary directory without links."""
    try:
        value = _safe_lstat(path)
        return stat.S_ISDIR(value.st_mode)
    except (FileNotFoundError, OSError, SyncError):
        return False


def _safe_empty_directory(path: Path) -> bool:
    """Return whether ``path`` is an ordinary, empty directory without links."""
    if not _safe_ordinary_directory(path):
        return False
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is None
    except OSError:
        return False


def _inactive_default_hook_samples(path: Path) -> bool:
    """Accept only ordinary ``*.sample`` files in Git's inactive default hook dir."""
    try:
        directory = _safe_lstat(path)
        if not stat.S_ISDIR(directory.st_mode):
            return False
        with os.scandir(path) as entries:
            for entry in entries:
                item = _safe_lstat(Path(entry.path))
                if not stat.S_ISREG(item.st_mode) or not entry.name.endswith(".sample"):
                    return False
        return True
    except FileNotFoundError:
        # A missing default hook directory has no executable hooks either.
        return True
    except (OSError, SyncError):
        return False


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


def _valid_snapshot_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 128
        and not any(char in value for char in "/\\\r\n\0")
        and ".." not in value
    )


def _snapshot_token(value: str, label: str) -> str:
    if not _valid_snapshot_token(value):
        raise SyncError("invalid_snapshot_token", f"Invalid snapshot {label}.", EXIT_CONFIGURATION)
    return value


def _manifest_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON members at every nesting level."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest member")
        result[key] = value
    return result


def _validated_manifest(payload: object) -> dict:
    """Validate committed manifest data without consulting the filesystem."""
    fields = {
        "schema_version", "created_at", "machine_id", "root_hash",
        "file_count", "total_bytes", "character_count", "files",
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("manifest schema")
    if payload["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ValueError("manifest version")
    if not isinstance(payload["created_at"], str) or not payload["created_at"]:
        raise ValueError("manifest timestamp")
    if not isinstance(payload["machine_id"], str) or not payload["machine_id"]:
        raise ValueError("manifest machine")
    if not isinstance(payload["root_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["root_hash"]):
        raise ValueError("manifest root hash")
    for name in ("file_count", "total_bytes", "character_count"):
        if isinstance(payload[name], bool) or not isinstance(payload[name], int) or payload[name] < 0:
            raise ValueError("manifest count")
    if not isinstance(payload["files"], list):
        raise ValueError("manifest files")

    files: list[dict[str, object]] = []
    folded: set[str] = set()
    previous_key: str | None = None
    for raw in payload["files"]:
        if not isinstance(raw, dict) or set(raw) != {"path", "size", "sha256"}:
            raise ValueError("manifest file schema")
        path, size, digest = raw["path"], raw["size"], raw["sha256"]
        if not isinstance(path, str) or validate_manifest_path(path) != path:
            raise ValueError("manifest file path")
        key = unicodedata.normalize("NFC", path).casefold()
        if key in folded or (previous_key is not None and key <= previous_key):
            raise ValueError("manifest file order")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("manifest file size")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("manifest file hash")
        folded.add(key)
        previous_key = key
        files.append({"path": path, "size": size, "sha256": digest})

    if payload["file_count"] != len(files):
        raise ValueError("manifest file count")
    if payload["total_bytes"] != sum(item["size"] for item in files):
        raise ValueError("manifest byte count")
    if payload["character_count"] != sum(1 for item in files if is_character_player_path(str(item["path"]))):
        raise ValueError("manifest character count")
    canonical = "\n".join(
        f"{item['path']}\0{item['size']}\0{item['sha256']}" for item in files
    ).encode()
    if hashlib.sha256(canonical).hexdigest() != payload["root_hash"]:
        raise ValueError("manifest root mismatch")
    return payload


@dataclass(frozen=True)
class GitResult:
    args: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int


@dataclass
class SnapshotValidationCache:
    """Build-scoped cache for immutable Git objects verified by exact OID."""
    manifests: dict[str, dict] = field(default_factory=dict)
    blobs: dict[str, tuple[int, str]] = field(default_factory=dict)


class GitRunner:
    """Small argv-only adapter; tests may inspect ``commands``."""
    def __init__(self, cwd: Path, executable: str = "git", *, timeout_seconds: float = _DEFAULT_GIT_TIMEOUT_SECONDS) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Git timeout must be a positive finite number.")
        self.cwd = Path(cwd).resolve(); self.executable = executable; self.commands: list[tuple[str, ...]] = []
        self.timeout_seconds = float(timeout_seconds)
        self.environment = os.environ.copy()
        # Keep key-based and configured credential-helper authentication intact,
        # but never permit a Git/GCM/SSH prompt.  stdin is also closed below, so
        # a malformed helper cannot fall back to a console prompt.
        self.environment.update({
            # Environment precedence prevents repository/global core.askPass
            # from launching an arbitrary UI. Git itself is a deterministic
            # non-interactive failure helper if credential helpers/SSH keys do
            # not already satisfy authentication.
            "GIT_ASKPASS": self.executable,
            "SSH_ASKPASS": self.executable,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "SSH_ASKPASS_REQUIRE": "never",
        })

    def _popen_options(self, *, text: bool, input_supplied: bool) -> dict[str, object]:
        options: dict[str, object] = {
            "cwd": self.cwd,
            "stdin": subprocess.PIPE if input_supplied else subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
            "env": self.environment,
            "text": text,
        }
        if text:
            options.update({"encoding": "utf-8", "errors": "replace"})
        if os.name == "nt":
            # A separate process group lets taskkill below reliably terminate
            # Git's SSH/GCM helper descendants after a timeout.
            options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return options

    @staticmethod
    def _terminate_timed_out_process(process: subprocess.Popen[object]) -> None:
        """Best-effort termination which includes Windows child processes."""
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=10,
                    shell=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            process.kill()
        except OSError:
            pass

    def _communicate(self, argv: tuple[str, ...], *, input_data: str | bytes | None, text: bool) -> tuple[str | bytes, str | bytes, int]:
        try:
            process = subprocess.Popen(
                list(argv),
                **self._popen_options(text=text, input_supplied=input_data is not None),
            )
        except OSError as error:
            raise SyncError("git_unavailable", "Git could not be executed.", EXIT_CONFIGURATION) from error
        try:
            stdout, stderr = process.communicate(input_data, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            self._terminate_timed_out_process(process)
            try:
                process.communicate(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self._raise_timeout(error)
        return stdout, stderr, process.returncode

    def _run_options(self) -> dict[str, object]:
        """Compatibility view for narrow test adapters; production uses Popen."""
        return {"cwd": self.cwd, "capture_output": True,
                "check": False, "timeout": self.timeout_seconds, "env": self.environment}

    @staticmethod
    def _raise_timeout(error: subprocess.TimeoutExpired) -> None:
        raise SyncError("git_timeout", "Git operation exceeded its safe time limit.", EXIT_CONFLICT) from error

    def run(self, *args: str, check: bool = True, input_text: str | None = None) -> GitResult:
        argv = (self.executable, *args); self.commands.append(argv)
        stdout, stderr, returncode = self._communicate(argv, input_data=input_text, text=True)
        assert isinstance(stdout, str) and isinstance(stderr, str)
        result = GitResult(tuple(args), stdout, stderr, returncode)
        if check and result.returncode:
            raise SyncError("git_command_failed", "Git command failed.", EXIT_CONFLICT, {"command": args[0] if args else "git"})
        return result

    def run_bytes(self, *args: str) -> bytes:
        argv = (self.executable, *args); self.commands.append(argv)
        stdout, _stderr, returncode = self._communicate(argv, input_data=None, text=False)
        assert isinstance(stdout, bytes)
        if returncode:
            raise SyncError("git_command_failed", "Git command failed.", EXIT_CONFLICT, {"command": args[0] if args else "git"})
        return stdout

    def run_bytes_input(self, *args: str, input_bytes: bytes) -> bytes:
        argv = (self.executable, *args); self.commands.append(argv)
        stdout, _stderr, returncode = self._communicate(argv, input_data=input_bytes, text=False)
        assert isinstance(stdout, bytes)
        if returncode:
            raise SyncError("git_command_failed", "Git command failed.", EXIT_CONFLICT, {"command": args[0] if args else "git"})
        return stdout

    def run_bytes_input_result(self, *args: str, input_bytes: bytes) -> GitResult:
        """Run Git with byte-exact stdin and retain a non-raising result.

        Windows text pipes translate LF to CRLF.  Object-building plumbing such
        as ``mktag`` must receive the canonical bytes, so it cannot use
        :meth:`run`'s text-mode stdin.
        """
        argv = (self.executable, *args); self.commands.append(argv)
        stdout, stderr, returncode = self._communicate(argv, input_data=input_bytes, text=False)
        assert isinstance(stdout, bytes) and isinstance(stderr, bytes)
        return GitResult(
            tuple(args),
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            returncode,
        )


@dataclass(frozen=True)
class VaultStatus:
    relation: str
    local_oid: str | None
    remote_oid: str | None


class GitVault:
    def __init__(self, repo: Path, *, remote: str = "origin", branch: str = "main", runner: GitRunner | None = None) -> None:
        self.repo = Path(repo).resolve(); self.remote = _remote_name(remote); self.branch = _branch_name(branch)
        self.runner = runner or GitRunner(self.repo)
        self.expected_remote_identity: tuple[str, str] | None = None

    def bind_remote_identity(self, expected: tuple[str, str]) -> None:
        try:
            actual = read_remote_identity(self, self.remote)
        except SyncError as error:
            raise SyncError("remote_identity_changed", "Remote fetch/push destination changed; no write was attempted.", EXIT_CONFLICT) from error
        if actual != expected:
            raise SyncError("remote_identity_changed", "Remote fetch/push destination changed; no write was attempted.", EXIT_CONFLICT)
        self.expected_remote_identity = expected

    def assert_remote_identity(self) -> None:
        if self.expected_remote_identity is not None:
            try:
                actual = read_remote_identity(self, self.remote)
            except SyncError as error:
                raise SyncError("remote_identity_changed", "Remote fetch/push destination changed; no write was attempted.", EXIT_CONFLICT) from error
            if actual != self.expected_remote_identity:
                raise SyncError("remote_identity_changed", "Remote fetch/push destination changed; no write was attempted.", EXIT_CONFLICT)

    def create_detached_snapshot(self, source: Path, *, machine_id: str, session_id: str,
                                 expected_manifest: dict, expected_remote_head: str,
                                 retries: int = 1, window_seconds: float = 0,
                                 validator=validate_players) -> str:
        """Write a verified snapshot commit using plumbing without moving a ref."""
        parent = _oid_value(expected_remote_head); expected = _validated_manifest(expected_manifest)
        machine_id = _snapshot_token(machine_id, "machine ID"); session_id = _snapshot_token(session_id, "session ID")
        if expected.get("machine_id") != machine_id or expected.get("file_count", 0) > 100000 or expected.get("total_bytes", 0) > 2 * 1024**3:
            raise SyncError("bookmark_live_too_large", "Live bookmark snapshot exceeds safe limits.", EXIT_VALIDATION)
        self.preflight(); self.assert_remote_identity()
        if self.remote_oid() != parent:
            raise SyncError("selection_stale", "Remote main changed before live bookmark construction.", EXIT_CONFLICT)
        current = stable_manifest(source, machine_id=machine_id, retries=retries, window_seconds=window_seconds)
        if current["root_hash"] != expected["root_hash"] or not validator(source, expected).get("ok"):
            raise SyncError("source_changed", "Live save changed before bookmark construction.", EXIT_VALIDATION)
        before_head = self._oid("HEAD")
        before_status = self.runner.run("status", "--porcelain=v1", "--untracked-files=all").stdout
        before_refs = self.runner.run("for-each-ref", "--format=%(refname)%00%(objectname)").stdout

        def blob(data: bytes) -> str:
            raw = self.runner.run_bytes_input("hash-object", "-w", "--stdin", input_bytes=data).decode("ascii", "strict").strip()
            return _oid_value(raw, "blob")
        tree: dict[str, Any] = {}
        for item in expected["files"]:
            relative = validate_manifest_path(str(item["path"])); path = assert_safe_save_file(Path(source), relative)
            data = path.read_bytes()
            if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise SyncError("source_changed", "Live save changed while bookmark objects were built.", EXIT_VALIDATION)
            node = tree
            parts = PurePosixPath(relative).parts
            for part in parts[:-1]: node = node.setdefault(part, {})
            node[parts[-1]] = ("blob", blob(data))
        manifest_blob = blob(json.dumps(expected, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        metadata = {"schema_version":"1.0.0", "machine_id":machine_id, "session_id":session_id, "root_hash":expected["root_hash"]}
        vault_blob = blob(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8"))

        def make_tree(node: dict[str, Any]) -> str:
            rows = bytearray()
            for name in sorted(node, key=lambda value: value.encode("utf-8")):
                if "\0" in name or "/" in name or not name: raise SyncError("unsafe_vault_tree", "Detached snapshot path was unsafe.", EXIT_VALIDATION)
                value = node[name]
                if isinstance(value, dict): kind, oid, mode = "tree", make_tree(value), "040000"
                else: kind, oid, mode = value[0], value[1], "100644"
                rows.extend(f"{mode} {kind} {oid}\t{name}".encode("utf-8") + b"\0")
            result = self.runner.run_bytes_input("mktree", "-z", input_bytes=bytes(rows)).decode("ascii", "strict").strip()
            return _oid_value(result, "tree")
        save_tree = make_tree(tree)
        sync_tree = make_tree({"manifest.json": ("blob", manifest_blob), "vault.json": ("blob", vault_blob)})
        root_rows = (f"040000 tree {sync_tree}\t.sync\0" + f"040000 tree {save_tree}\tsave\0").encode("utf-8")
        root_tree = _oid_value(self.runner.run_bytes_input("mktree", "-z", input_bytes=root_rows).decode("ascii", "strict").strip(), "tree")
        message = f"Detached save bookmark from {machine_id} root={expected['root_hash']} session={session_id}\n"
        commit = self.runner.run("commit-tree", root_tree, "-p", parent, input_text=message).stdout.strip()
        commit = _oid_value(commit)
        after = stable_manifest(source, machine_id=machine_id, retries=retries, window_seconds=window_seconds)
        if after["root_hash"] != expected["root_hash"] or self.remote_oid() != parent:
            raise SyncError("selection_stale", "Live or remote data changed before bookmark publication.", EXIT_CONFLICT)
        if self._oid("HEAD") != before_head or self.runner.run("status", "--porcelain=v1", "--untracked-files=all").stdout != before_status or self.runner.run("for-each-ref", "--format=%(refname)%00%(objectname)").stdout != before_refs:
            raise SyncError("detached_snapshot_mutated_vault", "Detached snapshot changed a protected Vault ref or worktree.", EXIT_RECOVERY_REQUIRED)
        committed = self.validate_commit_snapshot(commit)
        if committed["root_hash"] != expected["root_hash"]:
            raise SyncError("committed_save_mismatch", "Detached bookmark snapshot verification failed.", EXIT_RECOVERY_REQUIRED)
        return commit

    def align_head_to_remote(self, expected_remote_head: str) -> None:
        """After lock acquisition, move only an older clean local Vault to remote main."""
        expected = _oid_value(expected_remote_head)
        self.assert_remote_identity()
        branch = self.runner.run("symbolic-ref", "--quiet", "HEAD", check=False)
        if branch.returncode or branch.stdout.strip() != f"refs/heads/{self.branch}":
            raise SyncError("vault_branch_mismatch", "Vault HEAD is not the configured local branch.", EXIT_CONFLICT)
        self.preflight()
        if self.remote_oid() != expected or self._oid(self.remote_ref) != expected:
            raise SyncError("selection_stale", "Remote main changed before local Vault alignment.", EXIT_CONFLICT)
        head = self._oid("HEAD")
        if head is None or self.runner.run("merge-base", "--is-ancestor", head, expected, check=False).returncode:
            raise SyncError("vault_not_reconciled", "Local Vault cannot be safely aligned to selected remote main.", EXIT_CONFLICT)
        result = self.runner.run("merge", "--ff-only", expected, check=False)
        branch_after = self.runner.run("symbolic-ref", "--quiet", "HEAD", check=False)
        self.assert_remote_identity()
        remote_after = self.remote_oid()
        if (result.returncode or self._oid("HEAD") != expected or remote_after != expected
                or branch_after.returncode or branch_after.stdout.strip() != f"refs/heads/{self.branch}"):
            raise SyncError("vault_alignment_incomplete", "Local Vault alignment requires recovery.", EXIT_RECOVERY_REQUIRED)

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
        allowed_controlled_hooks = (
            hooks == ".sync/empty-hooks"
            and _safe_ordinary_directory(controlled_hooks.parent)
            and _safe_empty_directory(controlled_hooks)
        )
        allowed_default_hooks = not hooks and _inactive_default_hook_samples(default_hooks)
        if not (allowed_controlled_hooks or allowed_default_hooks):
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

    def remote_history(self, *, limit: int = 10) -> tuple[str, ...]:
        """Return newest-first, locally fetched remote-main commits.

        This is deliberately a read-only view.  Callers must fetch explicitly
        before using it, so catalog construction never hides a network or
        worktree mutation behind history enumeration.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise SyncError("invalid_history_limit", "History limit must be between 1 and 100.", EXIT_CONFIGURATION)
        result = self.runner.run("rev-list", "--max-count", str(limit), self.remote_ref, check=False)
        if result.returncode:
            # An absent remote branch is an empty history, while other Git
            # errors remain fail-closed.
            if self._oid(self.remote_ref) is None:
                return ()
            raise SyncError("git_command_failed", "Remote history could not be read.", EXIT_CONFLICT)
        commits = tuple(row.strip() for row in result.stdout.splitlines() if row.strip())
        if len(commits) > limit or any(not _OID.fullmatch(commit) for commit in commits) or len(set(commits)) != len(commits):
            raise SyncError("malformed_remote_history", "Remote history was malformed.", EXIT_CONFLICT)
        return commits

    def _materialize_remote_tags(
        self,
        expected: dict[str, tuple[str, str]],
        *,
        collision_code: str,
        fetch_code: str,
        mismatch_code: str,
    ) -> dict[str, str]:
        """Fetch bounded exact tag refs only after every local collision check."""
        missing: list[str] = []
        for ref, (tag_oid, _target) in expected.items():
            present = self.runner.run("rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()
            if present and present != tag_oid:
                raise SyncError(collision_code, "Local tag ref differs from its verified remote object.", EXIT_CONFLICT)
            if not present:
                missing.append(ref)
        for start in range(0, len(missing), _TAG_FETCH_BATCH_SIZE):
            chunk = missing[start:start + _TAG_FETCH_BATCH_SIZE]
            fetched = self.runner.run(
                "fetch", "--no-tags", self.remote, *(f"{ref}:{ref}" for ref in chunk), check=False,
            )
            if fetched.returncode:
                raise SyncError(fetch_code, "Verified remote tags could not be fetched.", EXIT_CONFLICT)
        verified: dict[str, str] = {}
        for ref, (tag_oid, target) in expected.items():
            object_oid = self.runner.run("rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()
            peeled = self.runner.run("rev-parse", "--verify", "--quiet", f"{ref}^{{}}", check=False).stdout.strip()
            if object_oid != tag_oid or peeled != target:
                raise SyncError(mismatch_code, "Remote tag target could not be verified.", EXIT_CONFLICT)
            verified[ref] = peeled
        return verified

    def legacy_annotated_tags(self, *, limit: int = 100) -> tuple[tuple[str, str], ...]:
        """Return verified local ``archive/*`` and ``milestone/*`` tag targets.

        Legacy tags are display-only catalog input; lightweight tags and any
        malformed ref/object row are rejected rather than interpreted.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise SyncError("invalid_history_limit", "Tag limit must be between 1 and 100.", EXIT_CONFIGURATION)
        result = self.runner.run(
            "for-each-ref", "--format=%(refname:short)\t%(objecttype)\t%(objectname)\t%(*objectname)",
            "refs/tags/archive", "refs/tags/milestone",
        )
        if len(result.stdout.encode("utf-8", "surrogatepass")) > _TAG_REF_OUTPUT_LIMIT:
            raise SyncError("legacy_tag_list_too_large", "Legacy bookmark ref output exceeded its safe bound.", EXIT_CONFLICT)
        rows: list[tuple[str, str]] = []
        for row in result.stdout.splitlines():
            fields = row.split("\t")
            if len(fields) != 4:
                raise SyncError("malformed_legacy_tag", "Legacy tag list was malformed.", EXIT_CONFLICT)
            name, object_type, tag_oid, peeled = fields
            if (
                not name.startswith(("archive/", "milestone/"))
                or object_type != "tag"
                or not _OID.fullmatch(tag_oid)
                or not _OID.fullmatch(peeled)
                or any(ord(char) < 32 for char in name)
                or len(f"refs/tags/{name}".encode("utf-8", "surrogatepass")) > _TAG_REF_BYTE_LIMIT
            ):
                raise SyncError("malformed_legacy_tag", "Legacy tag list was malformed.", EXIT_CONFLICT)
            rows.append((name, peeled))
            if len(rows) > _TAG_REF_COUNT_LIMIT:
                raise SyncError("legacy_tag_list_too_large", "Legacy bookmark count exceeded its safe bound.", EXIT_CONFLICT)
        # Tags are not fetched by the normal main-only fetch.  Discover remote
        # legacy tags explicitly, then fetch each exact immutable ref solely
        # to validate its annotated object and peeled commit locally.
        remote_result = self.runner.run(
            "ls-remote", self.remote, "refs/tags/archive/*", "refs/tags/milestone/*", check=False,
        )
        if remote_result.returncode:
            raise SyncError("legacy_tag_list_failed", "Legacy bookmark refs could not be read.", EXIT_CONFLICT)
        if len(remote_result.stdout.encode("utf-8", "surrogatepass")) > _TAG_REF_OUTPUT_LIMIT:
            raise SyncError("legacy_tag_list_too_large", "Legacy bookmark ref output exceeded its safe bound.", EXIT_CONFLICT)
        remote_values: dict[str, dict[str, str]] = {}
        for row in remote_result.stdout.splitlines():
            fields = row.split("\t")
            if len(fields) != 2:
                raise SyncError("malformed_legacy_tag", "Legacy tag list was malformed.", EXIT_CONFLICT)
            oid, ref = fields; peeled_remote = ref.endswith("^{}")
            base = ref[:-3] if peeled_remote else ref
            if (
                not re.fullmatch(r"refs/tags/(?:archive|milestone)/[^\x00-\x1f]+", base)
                or len(base.encode("utf-8", "surrogatepass")) > _TAG_REF_BYTE_LIMIT
                or not _OID.fullmatch(oid)
            ):
                raise SyncError("malformed_legacy_tag", "Legacy tag list was malformed.", EXIT_CONFLICT)
            item = remote_values.setdefault(base, {}); key = "peeled" if peeled_remote else "tag"
            if key in item:
                raise SyncError("malformed_legacy_tag", "Legacy tag list was malformed.", EXIT_CONFLICT)
            item[key] = oid
            if len(remote_values) > _TAG_REF_COUNT_LIMIT:
                raise SyncError("legacy_tag_list_too_large", "Legacy bookmark count exceeded its safe bound.", EXIT_CONFLICT)
        known = {name for name, _ in rows}
        selected: dict[str, tuple[str, str]] = {}
        for ref, item in sorted(remote_values.items()):
            if set(item) != {"tag", "peeled"}:
                raise SyncError("malformed_legacy_tag", "Legacy tag list was malformed.", EXIT_CONFLICT)
            short = ref.removeprefix("refs/tags/")
            if short not in known and len(rows) + len(selected) < limit:
                selected[ref] = (item["tag"], item["peeled"])
        verified = self._materialize_remote_tags(
            selected,
            collision_code="legacy_tag_local_collision",
            fetch_code="legacy_tag_fetch_failed",
            mismatch_code="legacy_tag_remote_mismatch",
        )
        for ref in selected:
            short = ref.removeprefix("refs/tags/")
            rows.append((short, verified[ref])); known.add(short)
        return tuple(rows[:limit])

    def _managed_bookmark_rows(self, *, remote: bool) -> tuple[tuple[str, str, str], ...]:
        """Return ``(short_name, tag_oid, peeled_commit)`` for managed tags.

        ``ls-remote`` is intentionally used without ``--refs``: annotated
        tags must have the matching peeled ``^{} `` row, otherwise a remote
        could make an unverifiable object look like a bookmark.
        """
        prefix = "refs/tags/grim-dawn-save-"
        if remote:
            result = self.runner.run("ls-remote", self.remote, f"{prefix}*", check=False)
        else:
            result = self.runner.run(
                "for-each-ref", "--format=%(refname)\t%(objectname)\t%(*objectname)", prefix,
            )
        if result.returncode:
            raise SyncError("bookmark_list_failed", "Managed bookmark refs could not be read.", EXIT_CONFLICT)
        if len(result.stdout.encode("utf-8", "surrogatepass")) > _TAG_REF_OUTPUT_LIMIT:
            raise SyncError("bookmark_list_too_large", "Managed bookmark ref output exceeded its safe bound.", EXIT_CONFLICT)
        values: dict[str, dict[str, str]] = {}
        for row in result.stdout.splitlines():
            fields = row.split("\t")
            if len(fields) != (2 if remote else 3):
                raise SyncError("malformed_bookmark_ref", "Managed bookmark refs were malformed.", EXIT_CONFLICT)
            if remote:
                oid, ref = fields
                peeled = ref.endswith("^{}")
                base = ref[:-3] if peeled else ref
                if not base.startswith(prefix) or not re.fullmatch(r"refs/tags/grim-dawn-save-[0-9a-f]{32}", base) or not _OID.fullmatch(oid):
                    raise SyncError("malformed_bookmark_ref", "Managed bookmark refs were malformed.", EXIT_CONFLICT)
                item = values.setdefault(base, {})
                key = "peeled" if peeled else "tag"
                if key in item:
                    raise SyncError("malformed_bookmark_ref", "Managed bookmark refs were malformed.", EXIT_CONFLICT)
                item[key] = oid
            else:
                ref, tag_oid, target = fields
                if (len(ref.encode("utf-8", "surrogatepass")) > _TAG_REF_BYTE_LIMIT
                        or not re.fullmatch(r"refs/tags/grim-dawn-save-[0-9a-f]{32}", ref)
                        or not _OID.fullmatch(tag_oid) or not _OID.fullmatch(target)):
                    raise SyncError("malformed_bookmark_ref", "Managed bookmark refs were malformed.", EXIT_CONFLICT)
                values[ref] = {"tag": tag_oid, "peeled": target}
            if len(values) > _TAG_REF_COUNT_LIMIT:
                raise SyncError("bookmark_list_too_large", "Managed bookmark count exceeded its safe bound.", EXIT_CONFLICT)
        rows: list[tuple[str, str, str]] = []
        for ref, item in sorted(values.items()):
            if set(item) != {"tag", "peeled"}:
                raise SyncError("malformed_bookmark_ref", "Managed bookmark refs were malformed.", EXIT_CONFLICT)
            rows.append((ref.removeprefix("refs/tags/"), item["tag"], item["peeled"]))
        return tuple(rows)

    def managed_bookmarks(self, *, limit: int = 100) -> tuple[tuple[str, str, str], ...]:
        """Fetch and verify remote managed tags, returning annotation strings.

        The returned short tag name is generated by this tool, never supplied
        by a user.  Fetching tags only materializes verified Git objects; it
        never changes main, live data, state, locks, or a worktree checkout.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise SyncError("invalid_history_limit", "Tag limit must be between 1 and 100.", EXIT_CONFIGURATION)
        rows = self._managed_bookmark_rows(remote=True)
        if len(rows) > limit:
            rows = rows[:limit]
        expected = {f"refs/tags/{name}": (tag_oid, target) for name, tag_oid, target in rows}
        self._materialize_remote_tags(
            expected,
            collision_code="bookmark_local_collision",
            fetch_code="bookmark_fetch_failed",
            mismatch_code="bookmark_remote_mismatch",
        )
        results: list[tuple[str, str, str]] = []
        for name, tag_oid, target in rows:
            ref = f"refs/tags/{name}"
            shown = self.runner.run("cat-file", "tag", ref, check=False)
            if shown.returncode or "\n\n" not in shown.stdout:
                raise SyncError("invalid_bookmark_annotation", "Managed bookmark annotation is invalid.", EXIT_VALIDATION)
            annotation = shown.stdout.split("\n\n", 1)[1]
            results.append((name, target, annotation))
        return tuple(results)

    def create_managed_bookmark(self, commit: str, annotation: str, *, detached_root_hash: str | None = None,
                                expected_remote_head: str | None = None,
                                expected_lock_oid: str | None = None,
                                publication_guard: Callable[[], None] | None = None,
                                publication_intent: Callable[[str, str, str], None] | None = None,
                                publication_confirmed: Callable[[str, str, str], None] | None = None) -> tuple[str, str, str]:
        """Create a UUID-named annotated tag and prove its remote target.

        The annotation has already been strict-validated by ``bookmarks``;
        this method still treats it solely as stdin data, never argv or shell.
        """
        self.assert_remote_identity()
        commit = _oid_value(commit)
        if self.runner.run("cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode:
            raise SyncError("invalid_bookmark_commit", "Bookmark commit is unavailable.", EXIT_VALIDATION)
        # Only a verified remote-main ancestor is eligible for ordinary use.
        remote = self.remote_oid()
        ordinary = remote is not None and self.runner.run("merge-base", "--is-ancestor", commit, remote, check=False).returncode == 0
        if not ordinary:
            if detached_root_hash is None or expected_remote_head != remote or not re.fullmatch(r"[0-9a-f]{64}", detached_root_hash):
                raise SyncError("bookmark_commit_not_allowed", "Bookmark commit is not a verified save version.", EXIT_VALIDATION)
            parents = self.runner.run("rev-list", "--parents", "-n", "1", commit, check=False).stdout.strip().split()
            manifest = self.validate_commit_snapshot(commit)
            containing = self.runner.run("for-each-ref", "--contains", commit, "--format=%(refname)").stdout.strip()
            if parents != [commit, remote] or manifest.get("root_hash") != detached_root_hash or containing:
                raise SyncError("bookmark_commit_not_allowed", "Detached bookmark snapshot was not authorized.", EXIT_VALIDATION)
        name = f"grim-dawn-save-{uuid.uuid4().hex}"
        ref = f"refs/tags/{name}"
        if self.runner.run("rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip():
            raise SyncError("bookmark_exists", "Managed bookmark ref already exists.", EXIT_CONFLICT)
        if publication_guard is not None:
            publication_guard()
        # Build the annotated tag object without creating a ref.  A hard crash
        # before publication intent persistence can therefore leave only an
        # unreachable object, never an untracked UUID-named local ref.
        tag_object = (
            f"object {commit}\n"
            "type commit\n"
            f"tag {name}\n"
            f"tagger Grim Dawn Save Sync <grim-dawn-sync@localhost> {int(time.time())} +0000\n"
            f"\n{annotation}\n"
        )
        made = self.runner.run_bytes_input_result("mktag", input_bytes=tag_object.encode("utf-8"))
        local_tag_oid = made.stdout.strip()
        if (
            made.returncode
            or not _OID.fullmatch(local_tag_oid)
            or self.runner.run("cat-file", "-t", local_tag_oid, check=False).stdout.strip() != "tag"
        ):
            raise SyncError("bookmark_create_failed", "Managed bookmark tag object was not created.", EXIT_RECOVERY_REQUIRED)
        if publication_intent is not None:
            publication_intent(name, local_tag_oid, commit)
        created = self.runner.run("update-ref", ref, local_tag_oid, "0" * len(local_tag_oid), check=False)
        actual_tag = self.runner.run("rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()
        actual_target = self.runner.run("rev-parse", "--verify", "--quiet", f"{ref}^{{}}", check=False).stdout.strip()
        if created.returncode or actual_tag != local_tag_oid or actual_target != commit:
            raise SyncError("bookmark_create_failed", "Managed bookmark ref was not created from its persisted object.", EXIT_RECOVERY_REQUIRED)
        self.assert_remote_identity()
        if publication_guard is not None:
            publication_guard()
        if expected_lock_oid is not None:
            lock_oid = _oid_value(expected_lock_oid, "lock")
            if expected_remote_head is None:
                raise SyncError("bookmark_lock_required", "Live bookmark publication requires an exact remote base.", EXIT_CONFLICT)
            self.runner.run(
                "push", "--atomic",
                f"--force-with-lease=refs/tags/grim-dawn-sync-active:{lock_oid}",
                f"--force-with-lease={self.remote_head}:{expected_remote_head}",
                self.remote,
                f"{expected_remote_head}:{self.remote_head}",
                f"{lock_oid}:refs/tags/grim-dawn-sync-active",
                f"{ref}:{ref}", check=False,
            )
        else:
            self.runner.run("push", self.remote, f"{ref}:{ref}", check=False)
        self.assert_remote_identity()
        if publication_guard is not None:
            publication_guard()
        remote_rows = {item[0]: item for item in self._managed_bookmark_rows(remote=True)}
        row = remote_rows.get(name)
        # The peeled commit alone is insufficient: a remote could retain a
        # different annotated tag object with a different user-visible note.
        # Matching object OIDs proves the local annotation below is exactly the
        # one stored by the remote immutable ref.
        if row is None or row[2] != commit or row[1] != local_tag_oid or not _OID.fullmatch(local_tag_oid):
            raise SyncError("bookmark_push_incomplete", "Managed bookmark remote object was not confirmed.", EXIT_RECOVERY_REQUIRED)
        if detached_root_hash is not None and self.validate_commit_snapshot(row[2]).get("root_hash") != detached_root_hash:
            raise SyncError("bookmark_push_incomplete", "Remote bookmark snapshot root was not confirmed.", EXIT_RECOVERY_REQUIRED)
        shown = self.runner.run("cat-file", "tag", ref, check=False)
        if shown.returncode or "\n\n" not in shown.stdout:
            raise SyncError("bookmark_remote_mismatch", "Managed bookmark annotation could not be verified.", EXIT_RECOVERY_REQUIRED)
        if publication_confirmed is not None:
            publication_confirmed(name, local_tag_oid, commit)
        return name, commit, shown.stdout.split("\n\n", 1)[1]

    def publish_managed_bookmark_intent(self, name: str, tag_oid: str, commit: str, root_hash: str, *,
                                        expected_remote_head: str, expected_lock_oid: str,
                                        publication_guard: Callable[[], None]) -> None:
        """Idempotently finish one exact persisted managed-tag publication."""
        if not re.fullmatch(r"grim-dawn-save-[0-9a-f]{32}", name):
            raise SyncError("invalid_bookmark_intent", "Managed bookmark intent ref is invalid.", EXIT_RECOVERY_REQUIRED)
        tag_oid = _oid_value(tag_oid, "tag"); commit = _oid_value(commit); lock_oid = _oid_value(expected_lock_oid, "lock")
        expected_remote_head = _oid_value(expected_remote_head); ref = f"refs/tags/{name}"
        if not re.fullmatch(r"[0-9a-f]{64}", root_hash):
            raise SyncError("invalid_bookmark_intent", "Managed bookmark intent root is invalid.", EXIT_RECOVERY_REQUIRED)
        object_type = self.runner.run("cat-file", "-t", tag_oid, check=False).stdout.strip()
        object_target = self.runner.run("rev-parse", "--verify", "--quiet", f"{tag_oid}^{{}}", check=False).stdout.strip()
        object_annotation = self.runner.run("cat-file", "tag", tag_oid, check=False)
        if (
            object_type != "tag"
            or object_target != commit
            or object_annotation.returncode
            or "\n\n" not in object_annotation.stdout
            or self.validate_commit_snapshot(commit).get("root_hash") != root_hash
        ):
            raise SyncError("invalid_bookmark_intent", "Persisted managed bookmark object is invalid.", EXIT_RECOVERY_REQUIRED)
        local_tag = self.runner.run("rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()
        if not local_tag:
            created = self.runner.run("update-ref", ref, tag_oid, "0" * len(tag_oid), check=False)
            local_tag = self.runner.run("rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()
            if created.returncode or local_tag != tag_oid:
                raise SyncError("invalid_bookmark_intent", "Persisted managed bookmark ref could not be created exactly.", EXIT_RECOVERY_REQUIRED)
        local_target = self.runner.run("rev-parse", "--verify", "--quiet", f"{ref}^{{}}", check=False).stdout.strip()
        local_type = self.runner.run("cat-file", "-t", ref, check=False).stdout.strip()
        local_annotation = self.runner.run("cat-file", "tag", ref, check=False)
        if (
            local_tag != tag_oid
            or local_target != commit
            or local_type != "tag"
            or local_annotation.returncode
            or "\n\n" not in local_annotation.stdout
        ):
            raise SyncError("invalid_bookmark_intent", "Local managed bookmark intent does not match its objects.", EXIT_RECOVERY_REQUIRED)
        publication_guard()
        self.runner.run(
            "push", "--atomic",
            f"--force-with-lease=refs/tags/grim-dawn-sync-active:{lock_oid}",
            f"--force-with-lease={self.remote_head}:{expected_remote_head}",
            self.remote,
            f"{expected_remote_head}:{self.remote_head}", f"{lock_oid}:refs/tags/grim-dawn-sync-active",
            f"{ref}:{ref}", check=False,
        )
        publication_guard()
        rows = {row[0]: row for row in self._managed_bookmark_rows(remote=True)}
        row = rows.get(name)
        if row is None or row[1] != tag_oid or row[2] != commit or self.validate_commit_snapshot(commit).get("root_hash") != root_hash:
            raise SyncError("bookmark_push_incomplete", "Persisted bookmark publication was not confirmed.", EXIT_RECOVERY_REQUIRED)

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

    def read_manifest(self, commit: str) -> dict:
        """Read and validate a committed manifest without extracting its save."""
        commit = _oid_value(commit)
        result = self.runner.run("show", f"{commit}:.sync/manifest.json", check=False)
        if result.returncode:
            raise SyncError(
                "invalid_remote_manifest",
                "Remote commit has no readable save manifest.",
                EXIT_VALIDATION,
            )
        try:
            payload = json.loads(result.stdout, object_pairs_hook=_manifest_object)
            return _validated_manifest(payload)
        except (json.JSONDecodeError, UnicodeError, ValueError, SyncError) as error:
            raise SyncError(
                "invalid_remote_manifest",
                "Remote commit save manifest is invalid.",
                EXIT_VALIDATION,
            ) from error

    def _read_blob_batch(self, oids: Iterable[str], cache: dict[str, tuple[int, str]]) -> None:
        """Hash validated blob OIDs through bounded ``cat-file --batch`` chunks."""
        missing = tuple(dict.fromkeys(oid for oid in oids if oid not in cache))
        if not missing:
            return
        pending: dict[str, tuple[int, str]] = {}
        batch_runner = getattr(self.runner, "run_bytes_input", None)
        if not callable(batch_runner):
            # Compatibility for narrow test adapters. Production GitRunner
            # always takes the bounded batch path.
            for oid in missing:
                blob = self.runner.run_bytes("cat-file", "blob", oid)
                pending[oid] = (len(blob), hashlib.sha256(blob).hexdigest())
            cache.update(pending)
            return
        for start in range(0, len(missing), _CAT_FILE_BATCH_SIZE):
            chunk = missing[start:start + _CAT_FILE_BATCH_SIZE]
            output = batch_runner("cat-file", "--batch", input_bytes=b"".join(
                oid.encode("ascii") + b"\n" for oid in chunk
            ))
            cursor = 0
            parsed: dict[str, tuple[int, str]] = {}
            for expected_oid in chunk:
                newline = output.find(b"\n", cursor)
                if newline < 0:
                    raise SyncError("invalid_remote_manifest", "Remote save blob batch was malformed.", EXIT_VALIDATION)
                try:
                    header = output[cursor:newline].decode("ascii", "strict").split(" ")
                    if len(header) != 3 or header[0] != expected_oid or header[1] != "blob" or not header[2].isdigit():
                        raise ValueError
                    size = int(header[2])
                except (UnicodeError, ValueError, OverflowError) as error:
                    raise SyncError("invalid_remote_manifest", "Remote save blob batch was malformed.", EXIT_VALIDATION) from error
                content_start = newline + 1
                content_end = content_start + size
                if content_end >= len(output) or output[content_end:content_end + 1] != b"\n":
                    raise SyncError("invalid_remote_manifest", "Remote save blob batch was malformed.", EXIT_VALIDATION)
                blob = output[content_start:content_end]
                parsed[expected_oid] = (size, hashlib.sha256(blob).hexdigest())
                cursor = content_end + 1
            if cursor != len(output):
                raise SyncError("invalid_remote_manifest", "Remote save blob batch was malformed.", EXIT_VALIDATION)
            pending.update(parsed)
        cache.update(pending)

    def _committed_save_files(self, commit: str, *, blob_cache: dict[str, tuple[int, str]] | None = None) -> dict[str, tuple[int, str]]:
        """Return the complete, verified ``save/`` blob table for a commit.

        This deliberately consumes NUL-delimited Git output and object bytes;
        it never checks out or otherwise materializes the committed tree.
        """
        try:
            listing = self.runner.run_bytes("ls-tree", "-r", "-z", commit, "--", "save")
        except SyncError as error:
            raise SyncError("invalid_remote_manifest", "Remote commit save tree could not be read.", EXIT_VALIDATION) from error
        paths: dict[str, str] = {}
        folded: set[str] = set()
        for raw in listing.split(b"\0"):
            if not raw:
                continue
            try:
                meta, encoded_path = raw.split(b"\t", 1)
                mode, kind, encoded_oid = meta.split()
                path = encoded_path.decode("utf-8", "strict")
                oid = encoded_oid.decode("ascii", "strict")
            except (ValueError, UnicodeError):
                raise SyncError("unsafe_vault_tree", "Vault tree entry was malformed.", EXIT_VALIDATION)
            if mode != b"100644" or kind != b"blob" or not _OID.fullmatch(oid) or not path.startswith("save/"):
                raise SyncError("unsafe_vault_tree", "Vault tree contains a non-file save entry.", EXIT_VALIDATION)
            try:
                relative = validate_manifest_path(path[5:])
            except SyncError as error:
                raise SyncError("unsafe_vault_tree", "Vault tree contains an unsafe path.", EXIT_VALIDATION) from error
            key = unicodedata.normalize("NFC", relative).casefold()
            if relative in paths or key in folded:
                raise SyncError("unsafe_vault_tree", "Vault tree has colliding paths.", EXIT_VALIDATION)
            folded.add(key)
            paths[relative] = oid
        verified = {} if blob_cache is None else blob_cache
        try:
            self._read_blob_batch(paths.values(), verified)
        except SyncError as error:
            if error.code == "invalid_remote_manifest":
                raise
            raise SyncError("invalid_remote_manifest", "Remote save blob could not be read.", EXIT_VALIDATION) from error
        return {path: verified[oid] for path, oid in paths.items()}

    def read_vault_metadata(self, commit: str) -> dict:
        """Read exact committed snapshot provenance without materializing it."""
        commit = _oid_value(commit)
        result = self.runner.run("show", f"{commit}:.sync/vault.json", check=False)
        if result.returncode:
            raise SyncError(
                "invalid_vault_metadata",
                "Committed snapshot provenance is missing.",
                EXIT_VALIDATION,
            )
        try:
            payload = json.loads(result.stdout, object_pairs_hook=_manifest_object)
        except (json.JSONDecodeError, UnicodeError, ValueError) as error:
            raise SyncError(
                "invalid_vault_metadata",
                "Committed snapshot provenance is invalid.",
                EXIT_VALIDATION,
            ) from error
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "machine_id", "session_id", "root_hash"}
            or payload.get("schema_version") != "1.0.0"
            or not _valid_snapshot_token(payload.get("machine_id"))
            or not _valid_snapshot_token(payload.get("session_id"))
            or not isinstance(payload.get("root_hash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", payload["root_hash"])
        ):
            raise SyncError(
                "invalid_vault_metadata",
                "Committed snapshot provenance is invalid.",
                EXIT_VALIDATION,
            )
        return payload

    def validate_commit_snapshot(self, commit: str, *, cache: SnapshotValidationCache | None = None) -> dict:
        """Fail closed unless every committed save blob matches its manifest."""
        commit = _oid_value(commit)
        if cache is not None and commit in cache.manifests:
            return cache.manifests[commit]
        manifest = self.read_manifest(commit)
        actual = self._committed_save_files(commit, blob_cache=None if cache is None else cache.blobs)
        declared = {str(item["path"]): (int(item["size"]), str(item["sha256"])) for item in manifest["files"]}
        if actual != declared:
            raise SyncError("committed_save_mismatch", "Committed save tree does not match its manifest.", EXIT_VALIDATION)
        if cache is not None:
            cache.manifests[commit] = manifest
        return manifest

    def snapshot(self, source: Path, *, machine_id: str, session_id: str, expected_manifest: dict | None = None, retries: int = 1, window_seconds: float = 0, validator=validate_players, state_hook=None) -> str:
        self.preflight()
        machine_id = _snapshot_token(machine_id, "machine ID"); session_id = _snapshot_token(session_id, "session ID")
        # New sync callers supply the manifest calculated while planning.  Keep
        # the old convenience API for direct callers, but never silently use a
        # caller supplied malformed manifest.
        expected = _validated_manifest(expected_manifest) if expected_manifest is not None else stable_manifest(source, machine_id=machine_id, retries=retries, window_seconds=window_seconds)
        if expected["machine_id"] != machine_id:
            raise SyncError("source_changed", "Snapshot manifest belongs to another machine.", EXIT_VALIDATION)
        current = stable_manifest(source, machine_id=machine_id, retries=retries, window_seconds=window_seconds)
        if current["root_hash"] != expected["root_hash"]:
            raise SyncError("source_changed", "Save source changed before snapshot.", EXIT_VALIDATION)
        validation = validator(source, expected)
        if not validation.get("ok"):
            raise SyncError(validation["classification"], "Save validation did not permit snapshot.", EXIT_VALIDATION)
        target = self.repo / "save"; stage = self.repo / ".sync" / f".save-stage-{session_id}"; rollback = self.repo / ".sync" / f".save-rollback-{session_id}"
        if stage.exists() or stage.is_symlink() or rollback.exists() or rollback.is_symlink(): raise SyncError("snapshot_name_collision", "Vault snapshot artifacts already exist.", EXIT_RECOVERY_REQUIRED)
        try: stage.parent.mkdir(exist_ok=True)
        except OSError as error: raise SyncError("snapshot_metadata_failed", "Vault snapshot metadata directory could not be prepared.", EXIT_RECOVERY_REQUIRED) from error
        if state_hook: state_hook("UPDATE_VAULT")
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
            if state_hook: state_hook("COMMIT")
            message = f"Save session from {machine_id} root={expected['root_hash']} session={session_id}"
            if self.runner.run("commit", "-m", message, check=False).returncode:
                raise SyncError("git_commit_failed", "Vault snapshot commit failed; rollback artifact was retained.", EXIT_RECOVERY_REQUIRED)
        oid = self._oid("HEAD")
        if oid is None: raise SyncError("git_commit_failed", "Vault did not produce a commit.", EXIT_RECOVERY_REQUIRED)
        try:
            self.validate_commit_snapshot(oid)
        except SyncError as error:
            raise SyncError("committed_save_mismatch", "Committed snapshot verification failed; rollback artifact was retained.", EXIT_RECOVERY_REQUIRED) from error
        if rollback.exists():
            try:
                _remove_safe_tree(rollback)
            except (OSError, SyncError) as error:
                raise SyncError("snapshot_cleanup_incomplete", "Vault commit succeeded but rollback artifact was retained.", EXIT_RECOVERY_REQUIRED) from error
        return oid

    def push(self, expected_oid: str | None = None) -> str:
        self.assert_remote_identity()
        oid = self._oid("HEAD")
        if oid is None: raise SyncError("vault_unborn", "Vault has no local commit to push.", EXIT_CONFIGURATION)
        if expected_oid and oid != expected_oid: raise SyncError("vault_head_changed", "Vault HEAD changed before push.", EXIT_CONFLICT)
        self.assert_remote_identity()
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
        if ancestry == 1:
            managed_targets = {target for _, target, _ in self.managed_bookmarks(limit=100)}
            if commit not in managed_targets:
                raise SyncError("restore_commit_not_in_history", "Restore commit is not in vault history or a verified managed bookmark.", EXIT_VALIDATION)
        elif ancestry != 0:
            raise SyncError("git_command_failed", "Restore commit ancestry could not be verified.", EXIT_CONFLICT)
        manifest = self.validate_commit_snapshot(commit)
        _safe_destination_ancestors(destination)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise SyncError("historical_extract_failed", "Historical save parent could not be created safely.", EXIT_VALIDATION) from error
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
            extracted_manifest = stable_manifest(stage, machine_id=machine_id, retries=retries, window_seconds=window_seconds)
            if extracted_manifest["root_hash"] != manifest["root_hash"]:
                try: _remove_safe_tree(stage)
                except SyncError as error: raise SyncError("historical_extract_mismatch", "Extracted save mismatch could not be safely removed.", EXIT_RECOVERY_REQUIRED) from error
                raise SyncError("historical_extract_mismatch", "Extracted save did not match committed manifest.", EXIT_VALIDATION)
            validation = validator(stage, extracted_manifest)
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
        try:
            published = stable_manifest(destination, machine_id=machine_id, retries=retries, window_seconds=window_seconds)
        except OSError as error:
            raise SyncError("historical_extract_validation_failed", "Published historical save could not be verified.", EXIT_RECOVERY_REQUIRED) from error
        if published["root_hash"] != manifest["root_hash"]:
            try: _remove_safe_tree(destination)
            except SyncError as error: raise SyncError("historical_extract_mismatch", "Published save mismatch could not be safely removed.", EXIT_RECOVERY_REQUIRED) from error
            raise SyncError("historical_extract_mismatch", "Published historical save did not match committed manifest.", EXIT_VALIDATION)
        return destination
