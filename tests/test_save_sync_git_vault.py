from __future__ import annotations

import io
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from grim_dawn_sync.errors import EXIT_RECOVERY_REQUIRED, SyncError
from grim_dawn_sync.git_vault import GitResult, GitRunner, GitVault
from grim_dawn_sync.manifest import stable_manifest


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, encoding="utf-8", capture_output=True).stdout


def clone_pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    remote = tmp_path / "remote.git"; git(tmp_path, "init", "--bare", str(remote))
    first, second = tmp_path / "one", tmp_path / "two"
    git(tmp_path, "clone", str(remote), str(first)); git(tmp_path, "clone", str(remote), str(second))
    for repo in (first, second):
        git(repo, "config", "user.name", "test"); git(repo, "config", "user.email", "test@example.invalid")
        (repo / ".sync" / "empty-hooks").mkdir(parents=True)
        git(repo, "config", "core.hooksPath", ".sync/empty-hooks")
    return remote, first, second


def save(root: Path, value: bytes = b"x") -> Path:
    (root / "main" / "a").mkdir(parents=True, exist_ok=True)
    (root / "main" / "a" / "player.gdc").write_bytes(value)
    return root


def valid(*_): return {"ok": True}


def checkout_remote_main(repo: Path) -> None:
    git(repo, "fetch", "origin")
    git(repo, "checkout", "-b", "main", "origin/main")


def commands(vault: GitVault) -> list[tuple[str, ...]]:
    return [tuple(command[1:]) for command in vault.runner.commands]


def assert_no_forbidden(vault: GitVault) -> None:
    flattened = [argument for command in vault.runner.commands for argument in command]
    assert not any(argument in {"pull", "rebase", "reset", "--force", "checkout"} for argument in flattened)


class ArchiveRunner:
    def __init__(self, listing: str, payload: bytes) -> None:
        self.listing = listing; self.payload = payload; self.commands: list[tuple[str, ...]] = []; self.cwd = Path.cwd(); self.executable = "git"

    def _manifest(self) -> str:
        files: list[dict[str, object]] = []
        if not self.payload:
            return json.dumps({"schema_version": "1.0.0", "created_at": "x", "machine_id": "a", "root_hash": hashlib.sha256(b"").hexdigest(), "file_count": 0, "total_bytes": 0, "character_count": 0, "files": []})
        with tarfile.open(fileobj=io.BytesIO(self.payload), mode="r:") as archive:
            for member in archive.getmembers():
                if not member.isfile() or not member.name.startswith("save/"):
                    continue
                stream = archive.extractfile(member); assert stream is not None
                data = stream.read(); relative = member.name[5:]
                files.append({"path": relative, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        files.sort(key=lambda item: str(item["path"]).casefold())
        canonical = "\n".join(f"{item['path']}\0{item['size']}\0{item['sha256']}" for item in files).encode()
        return json.dumps({"schema_version": "1.0.0", "created_at": "x", "machine_id": "a", "root_hash": hashlib.sha256(canonical).hexdigest(), "file_count": len(files), "total_bytes": sum(int(item["size"]) for item in files), "character_count": len(files), "files": files})

    def run(self, *args: str, check: bool = True, input_text: str | None = None) -> GitResult:
        self.commands.append(("git", *args))
        stdout = self.listing if args and args[0] == "ls-tree" else self._manifest() if args and args[0] == "show" else ""
        return GitResult(tuple(args), stdout, "", 0)

    def run_bytes(self, *args: str) -> bytes:
        self.commands.append(("git", *args))
        if args and args[0] == "ls-tree": return self.listing.encode()
        if args[:2] == ("cat-file", "blob"):
            with tarfile.open(fileobj=io.BytesIO(self.payload), mode="r:") as archive:
                for member in archive.getmembers():
                    if member.isfile():
                        stream = archive.extractfile(member); assert stream is not None; return stream.read()
            return b""
        return self.payload


def tar_payload(entries: list[tuple[str, str, bytes | None]]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        root = tarfile.TarInfo("save"); root.type = tarfile.DIRTYPE; archive.addfile(root)
        for name, kind, payload in entries:
            member = tarfile.TarInfo(name)
            if kind == "dir": member.type = tarfile.DIRTYPE; archive.addfile(member)
            elif kind == "symlink": member.type = tarfile.SYMTYPE; member.linkname = "../../outside"; archive.addfile(member)
            elif kind == "hardlink": member.type = tarfile.LNKTYPE; member.linkname = "save/main/a/player.gdc"; archive.addfile(member)
            else:
                assert payload is not None; member.size = len(payload); archive.addfile(member, io.BytesIO(payload))
    return stream.getvalue()


def tree_listing(*paths: str, mode: str = "100644") -> str:
    oid = "a" * 40
    return "".join(f"{mode} blob {oid}\t{path}\0" for path in paths)


def test_unborn_snapshot_push_then_other_clone_fast_forwards(tmp_path: Path) -> None:
    _, one, two = clone_pair(tmp_path); source = save(tmp_path / "source")
    vault = GitVault(one)
    commit = vault.snapshot(source, machine_id="a", session_id="s", validator=valid)
    assert vault.push(commit) == commit
    other = GitVault(two); status = other.update_fast_forward()
    assert status.relation == "unborn"
    git(two, "fetch", "origin"); git(two, "checkout", "-b", "main", "origin/main")
    assert GitVault(two).reconcile().relation == "equal"


def test_ahead_behind_and_diverged_are_classified_without_rebase_or_force(tmp_path: Path) -> None:
    _, one, two = clone_pair(tmp_path); source = save(tmp_path / "source")
    first = GitVault(one); first.push(first.snapshot(source, machine_id="a", session_id="1", validator=valid))
    git(two, "fetch", "origin"); git(two, "checkout", "-b", "main", "origin/main")
    (two / "save/main/a/player.gdc").write_bytes(b"two"); git(two, "add", "save/main/a/player.gdc"); git(two, "commit", "-m", "two")
    assert GitVault(two).reconcile().relation == "ahead"
    save(source, b"new")
    # Snapshot owns only its managed paths and can make an independent remote advance.
    first.push(first.snapshot(source, machine_id="a", session_id="2", validator=valid))
    second_vault = GitVault(two)
    with pytest.raises(SyncError, match="diverged"): second_vault.update_fast_forward()
    assert not any(any(word in command for word in ("rebase", "reset", "pull", "--force")) for command in second_vault.runner.commands)


def test_preflight_rejects_unmanaged_dirty_and_hooks(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path); (one / "unmanaged").write_text("x", encoding="utf-8")
    with pytest.raises(SyncError, match="unmanaged"): GitVault(one).preflight()
    (one / "unmanaged").unlink(); (one / ".sync" / "empty-hooks" / "pre-commit").write_text("x", encoding="utf-8")
    with pytest.raises(SyncError, match="hooks"): GitVault(one).preflight()


def test_preflight_allows_fresh_clone_default_inactive_hook_samples(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"; git(tmp_path, "init", "--bare", str(remote))
    fresh = tmp_path / "fresh"; git(tmp_path, "clone", str(remote), str(fresh))

    hooks = fresh / ".git" / "hooks"
    assert any(path.suffix == ".sample" for path in hooks.iterdir())
    GitVault(fresh).preflight()


def test_preflight_rejects_active_default_hook_and_custom_hookspath(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path)
    git(one, "config", "--unset", "core.hooksPath")
    hooks = one / ".git" / "hooks"
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    with pytest.raises(SyncError) as caught:
        GitVault(one).preflight()
    assert caught.value.code == "vault_hooks_present"

    (hooks / "pre-commit").unlink()
    custom = one / ".sync" / "other-hooks"; custom.mkdir()
    git(one, "config", "core.hooksPath", ".sync/other-hooks")
    with pytest.raises(SyncError) as caught:
        GitVault(one).preflight()
    assert caught.value.code == "vault_hooks_present"

    git(one, "config", "--unset", "core.hooksPath")


def test_preflight_rejects_linked_default_sample_hook(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path)
    git(one, "config", "--unset", "core.hooksPath")
    hooks = one / ".git" / "hooks"
    linked = hooks / "pre-push.sample"
    target = tmp_path / "outside-sample"; target.write_text("not a hook", encoding="utf-8")
    linked.unlink()
    try:
        linked.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links unavailable in this test environment")
    with pytest.raises(SyncError) as caught:
        GitVault(one).preflight()
    assert caught.value.code == "vault_hooks_present"


def test_preflight_rejects_reparse_marked_default_sample_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import grim_dawn_sync.git_vault as module

    _, one, _ = clone_pair(tmp_path)
    git(one, "config", "--unset", "core.hooksPath")
    sample = one / ".git" / "hooks" / "pre-commit.sample"
    original = module._safe_lstat

    def unsafe_sample(path: Path):
        if Path(path) == sample:
            raise SyncError("unsafe_vault_tree", "reparse", EXIT_RECOVERY_REQUIRED)
        return original(path)

    monkeypatch.setattr(module, "_safe_lstat", unsafe_sample)
    with pytest.raises(SyncError) as caught:
        GitVault(one).preflight()
    assert caught.value.code == "vault_hooks_present"


@pytest.mark.parametrize("unsafe_path", [".git/hooks", ".sync"])
def test_preflight_rejects_reparse_hook_directory_or_controlled_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe_path: str
) -> None:
    import grim_dawn_sync.git_vault as module

    _, one, _ = clone_pair(tmp_path)
    if unsafe_path == ".git/hooks":
        git(one, "config", "--unset", "core.hooksPath")
    unsafe = one / unsafe_path
    original = module._safe_lstat

    def unsafe_directory(path: Path):
        if Path(path) == unsafe:
            raise SyncError("unsafe_vault_tree", "reparse", EXIT_RECOVERY_REQUIRED)
        return original(path)

    monkeypatch.setattr(module, "_safe_lstat", unsafe_directory)
    with pytest.raises(SyncError) as caught:
        GitVault(one).preflight()
    assert caught.value.code == "vault_hooks_present"


def test_extracts_past_save_without_checkout_and_rejects_bad_destination(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path); source = save(tmp_path / "source", b"old"); vault = GitVault(one)
    old = vault.snapshot(source, machine_id="a", session_id="1", validator=valid)
    save(source, b"new"); vault.snapshot(source, machine_id="a", session_id="2", validator=valid)
    destination = tmp_path / "restored"; vault.extract_save(old, destination, machine_id="a", validator=valid)
    assert (destination / "main/a/player.gdc").read_bytes() == b"old"
    assert not any(command and command[0] == "checkout" for command in vault.runner.commands)


def test_read_manifest_is_read_only_and_does_not_extract_or_stage(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path)
    vault = GitVault(one)
    commit = vault.snapshot(save(tmp_path / "source"), machine_id="a", session_id="one", validator=valid)
    local_root = tmp_path / "terminal-local"
    before = {
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
    }
    command_index = len(vault.runner.commands)

    manifest = vault.read_manifest(commit)

    after = {
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
    }
    assert manifest["root_hash"] and manifest["file_count"] == 1
    assert before == after and not local_root.exists()
    assert commands(vault)[command_index:] == [
        ("show", f"{commit}:.sync/manifest.json"),
    ]


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [
        ("", 1),
        ('{"schema_version":"1.0.0","schema_version":"1.0.0"}', 0),
        ('{"schema_version":"1.0.0"}', 0),
        ('{"schema_version":"1.0.0","created_at":"x","machine_id":"x","root_hash":"BAD","file_count":0,"total_bytes":0,"character_count":0,"files":[]}', 0),
    ],
)
def test_read_manifest_missing_duplicate_or_malformed_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int
) -> None:
    vault = GitVault(tmp_path)
    monkeypatch.setattr(
        vault.runner,
        "run",
        lambda *args, **kwargs: GitResult(tuple(args), stdout, "missing", returncode),
    )

    with pytest.raises(SyncError) as caught:
        vault.read_manifest("a" * 40)

    assert caught.value.code == "invalid_remote_manifest"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema_version":"1.0.0","machine_id":"a","machine_id":"a","session_id":"s","root_hash":"' + "d" * 64 + '"}',
        '{"schema_version":"1.0.0","machine_id":"a","session_id":"s","root_hash":"' + "d" * 64 + '","extra":true}',
        '{"schema_version":"1.0.0","machine_id":"a","session_id":"../bad","root_hash":"' + "d" * 64 + '"}',
        '{"schema_version":"1.0.0","machine_id":"a","session_id":"s","root_hash":"BAD"}',
    ],
)
def test_read_vault_metadata_rejects_duplicate_schema_token_and_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    vault = GitVault(tmp_path)
    monkeypatch.setattr(
        vault.runner,
        "run",
        lambda *args, **kwargs: GitResult(tuple(args), payload, "", 0),
    )

    with pytest.raises(SyncError) as caught:
        vault.read_vault_metadata("a" * 40)

    assert caught.value.code == "invalid_vault_metadata"
    assert list(tmp_path.iterdir()) == []


def test_invalid_identifiers_and_remote_confirmation_failure_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, one, _ = clone_pair(tmp_path)
    with pytest.raises(SyncError): GitVault(one, branch="main:evil")
    source = save(tmp_path / "source"); vault = GitVault(one); commit = vault.snapshot(source, machine_id="a", session_id="1", validator=valid)
    monkeypatch.setattr(vault, "remote_oid", lambda: "0" * 40)
    with pytest.raises(SyncError, match="confirmed"): vault.push(commit)
    assert vault._oid("HEAD") == commit


@pytest.mark.parametrize("remote", ["/origin", "team/origin", "-origin", ".origin", "origin..backup", "origin\nbackup"])
def test_remote_name_uses_single_strict_component(tmp_path: Path, remote: str) -> None:
    with pytest.raises(SyncError) as caught: GitVault(tmp_path, remote=remote)
    assert caught.value.code == "invalid_git_identifier"


@pytest.mark.parametrize("branch", ["/main", "main/", "a//b", "a.lock", "@", "a/../b", "a/.hidden", "main\x01"])
def test_branch_name_rejects_invalid_git_ref_forms(tmp_path: Path, branch: str) -> None:
    with pytest.raises(SyncError) as caught: GitVault(tmp_path, branch=branch)
    assert caught.value.code == "invalid_git_identifier"


def test_branch_name_allows_hierarchical_ref(tmp_path: Path) -> None:
    vault = GitVault(tmp_path, remote="origin-2", branch="feature/x")
    assert vault.remote == "origin-2" and vault.remote_head == "refs/heads/feature/x"


def test_actual_behind_update_uses_ff_only_and_finishes_equal(tmp_path: Path) -> None:
    _, one, two = clone_pair(tmp_path); source = save(tmp_path / "source", b"base"); first = GitVault(one)
    first.push(first.snapshot(source, machine_id="a", session_id="base", validator=valid)); checkout_remote_main(two)
    save(source, b"remote-new"); remote_commit = first.snapshot(source, machine_id="a", session_id="next", validator=valid); first.push(remote_commit)
    second = GitVault(two); before = second._oid("HEAD"); result = second.update_fast_forward()
    assert result.relation == "behind" and before != remote_commit
    assert second._oid("HEAD") == remote_commit and second.reconcile().relation == "equal"
    assert ("merge", "--ff-only", "refs/remotes/origin/main") in commands(second)
    assert_no_forbidden(second)


def test_non_fast_forward_push_keeps_losing_commit_and_remote_winner(tmp_path: Path) -> None:
    _, one, two = clone_pair(tmp_path); base = save(tmp_path / "base"); first = GitVault(one)
    first.push(first.snapshot(base, machine_id="a", session_id="base", validator=valid)); checkout_remote_main(two)
    a_source, b_source = save(tmp_path / "a-source", b"a"), save(tmp_path / "b-source", b"b")
    a_commit = first.snapshot(a_source, machine_id="a", session_id="a", validator=valid)
    second = GitVault(two); b_commit = second.snapshot(b_source, machine_id="b", session_id="b", validator=valid)
    first.push(a_commit)
    with pytest.raises(SyncError) as caught: second.push(b_commit)
    assert caught.value.code == "push_incomplete" and caught.value.exit_code == EXIT_RECOVERY_REQUIRED
    assert second._oid("HEAD") == b_commit and first.remote_oid() == a_commit
    assert_no_forbidden(first); assert_no_forbidden(second)


def test_snapshot_copy_and_promote_failures_preserve_existing_save_and_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from grim_dawn_sync import git_vault as module
    _, one, _ = clone_pair(tmp_path); old_source = save(tmp_path / "old", b"old"); vault = GitVault(one)
    old_head = vault.snapshot(old_source, machine_id="a", session_id="old", validator=valid)
    new_source = save(tmp_path / "new", b"new")
    monkeypatch.setattr(module, "_copy_verified", lambda *_args, **_kwargs: (_ for _ in ()).throw(SyncError("copy_failed", "copy", 3)))
    with pytest.raises(SyncError, match="copy"): vault.snapshot(new_source, machine_id="a", session_id="copy", validator=valid)
    assert (one / "save/main/a/player.gdc").read_bytes() == b"old" and vault._oid("HEAD") == old_head
    monkeypatch.setattr(module, "_copy_verified", __import__("grim_dawn_sync.snapshot", fromlist=["_copy_verified"])._copy_verified)
    original_rename = module.os.rename
    def fail_promote(source: Path, destination: Path) -> None:
        if Path(source).name.startswith(".save-stage-"): raise OSError("promote")
        original_rename(source, destination)
    monkeypatch.setattr(module.os, "rename", fail_promote)
    with pytest.raises(SyncError) as caught: vault.snapshot(new_source, machine_id="a", session_id="promote", validator=valid)
    assert caught.value.code == "snapshot_swap_failed" and caught.value.exit_code == EXIT_RECOVERY_REQUIRED
    assert (one / "save/main/a/player.gdc").read_bytes() == b"old" and vault._oid("HEAD") == old_head


@pytest.mark.parametrize("failure", ["metadata", "commit"])
def test_snapshot_metadata_and_commit_failures_keep_recovery_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    from grim_dawn_sync import git_vault as module
    _, one, _ = clone_pair(tmp_path); vault = GitVault(one); old = save(tmp_path / "old", b"old")
    old_head = vault.snapshot(old, machine_id="a", session_id="old", validator=valid); new = save(tmp_path / "new", b"new")
    if failure == "metadata":
        monkeypatch.setattr(module.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("replace")))
    else:
        original_run = vault.runner.run
        def fail_commit(*args: str, **kwargs) -> GitResult:
            return GitResult(tuple(args), "", "", 1) if args and args[0] == "commit" else original_run(*args, **kwargs)
        monkeypatch.setattr(vault.runner, "run", fail_commit)
    with pytest.raises(SyncError) as caught: vault.snapshot(new, machine_id="a", session_id=failure, validator=valid)
    assert caught.value.exit_code == EXIT_RECOVERY_REQUIRED and vault._oid("HEAD") == old_head
    assert (one / ".sync" / f".save-rollback-{failure}").is_dir()
    assert (one / "save/main/a/player.gdc").read_bytes() == b"new"


@pytest.mark.parametrize("name", ["README.md", "note.txt"])
def test_preflight_rejects_clean_tracked_unmanaged_paths(tmp_path: Path, name: str) -> None:
    _, one, _ = clone_pair(tmp_path); vault = GitVault(one); vault.snapshot(save(tmp_path / "source"), machine_id="a", session_id="one", validator=valid)
    (one / name).write_text("tracked", encoding="utf-8"); git(one, "add", name); git(one, "commit", "-m", "unmanaged")
    with pytest.raises(SyncError) as caught: vault.preflight()
    assert caught.value.code == "vault_unmanaged_tracked"


@pytest.mark.parametrize("output", ["garbage\n", "abc\trefs/heads/main\n", f"{'a' * 40}\trefs/heads/other\n", f"{'a' * 40}\trefs/heads/main\n{'b' * 40}\trefs/heads/main\n"])
def test_remote_oid_rejects_malformed_multiple_and_mismatched_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    _, one, _ = clone_pair(tmp_path); vault = GitVault(one)
    monkeypatch.setattr(vault.runner, "run", lambda *args, **kwargs: GitResult(tuple(args), output, "", 0))
    with pytest.raises(SyncError) as caught: vault.remote_oid()
    assert caught.value.code == "malformed_remote_ref"


def test_extract_rejects_symbolic_nonhex_and_unrelated_commit_ids(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path); vault = GitVault(one); vault.snapshot(save(tmp_path / "source"), machine_id="a", session_id="one", validator=valid)
    for index, commit in enumerate(("HEAD", "refs/heads/main", "g" * 40, "a" * 39)):
        with pytest.raises(SyncError) as caught: vault.extract_save(commit, tmp_path / f"dest-{index}", machine_id="a", validator=valid)
        assert caught.value.code == "invalid_git_identifier"
    unrelated = git(one, "commit-tree", "4b825dc642cb6eb9a060e54bf8d69288fbee4904", "-m", "unrelated").strip()
    with pytest.raises(SyncError) as caught: vault.extract_save(unrelated, tmp_path / "unrelated", machine_id="a", validator=valid)
    assert caught.value.code == "restore_commit_not_in_history"


@pytest.mark.parametrize("listing", ["malformed\0", tree_listing("save/main/a/player.gdc", mode="120000")])
def test_extract_rejects_malformed_and_symlink_git_tree(tmp_path: Path, listing: str) -> None:
    runner = ArchiveRunner(listing, b""); vault = GitVault(tmp_path, runner=runner)
    with pytest.raises(SyncError) as caught: vault.extract_save("a" * 40, tmp_path / "destination", machine_id="a", validator=valid)
    assert caught.value.code in {"unsafe_vault_tree", "committed_save_mismatch"} and not (tmp_path / "destination").exists()


@pytest.mark.parametrize("entries", [
    [("save/main", "dir", None), ("save/main/a", "dir", None), ("save/main/a/player.gdc", "file", b"x"), ("save/../outside", "file", b"bad")],
    [("save/main", "dir", None), ("save/main/a", "dir", None), ("save/main/a/player.gdc", "symlink", None)],
    [("save/main", "dir", None), ("save/main/a", "dir", None), ("save/main/a/player.gdc", "hardlink", None)],
    [("save/main", "dir", None), ("save/main/a", "dir", None), ("save/main/a/player.gdc", "file", b"x"), ("save/MAIN/A/PLAYER.GDC", "file", b"y")],
    [("save/main", "file", b"x"), ("save/main/a/player.gdc", "file", b"y")],
])
def test_mocked_malicious_tar_never_creates_destination_or_writes_outside(tmp_path: Path, entries) -> None:
    outside = tmp_path / "outside"; listing = tree_listing("save/main/a/player.gdc")
    runner = ArchiveRunner(listing, tar_payload(entries)); vault = GitVault(tmp_path, runner=runner); destination = tmp_path / "destination"
    with pytest.raises(SyncError) as caught: vault.extract_save("a" * 40, destination, machine_id="a", validator=valid)
    assert caught.value.code in {"unsafe_vault_tree", "invalid_remote_manifest", "committed_save_mismatch"} and not destination.exists() and not outside.exists()


def test_extract_rejects_dangling_destination_symlink_without_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import grim_dawn_sync.git_vault as module
    destination = tmp_path / "dangling"; original = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0) if path == destination else original(path))
    runner = ArchiveRunner(tree_listing("save/main/a/player.gdc"), tar_payload([("save/main", "dir", None), ("save/main/a", "dir", None), ("save/main/a/player.gdc", "file", b"x")]))
    with pytest.raises(SyncError) as caught: GitVault(tmp_path, runner=runner).extract_save("a" * 40, destination, machine_id="a", validator=valid)
    assert caught.value.code == "unsafe_vault_tree" and not destination.exists()


def test_normal_historical_extraction_and_forbidden_command_source_guard(tmp_path: Path) -> None:
    _, one, _ = clone_pair(tmp_path); vault = GitVault(one); old = vault.snapshot(save(tmp_path / "source", b"old"), machine_id="a", session_id="old", validator=valid)
    vault.snapshot(save(tmp_path / "source", b"new"), machine_id="a", session_id="new", validator=valid)
    result = vault.extract_save(old, tmp_path / "restored-normal", machine_id="a", validator=valid)
    assert (result / "main/a/player.gdc").read_bytes() == b"old"
    assert_no_forbidden(vault)
    source = (Path(__file__).parents[1] / "src/grim_dawn_sync/git_vault.py").read_text(encoding="utf-8")
    for forbidden in ('"pull"', '"rebase"', '"reset"', '"--force"', '"checkout"'):
        assert forbidden not in source
    assert "shell=False" in source


def atomic_archive_runner(tmp_path: Path) -> ArchiveRunner:
    entries = [("save/main", "dir", None), ("save/main/a", "dir", None), ("save/main/a/player.gdc", "file", b"atomic")]
    return ArchiveRunner(tree_listing("save/main/a/player.gdc"), tar_payload(entries))


def retained_extract_stages(tmp_path: Path) -> list[Path]:
    return list(tmp_path.glob(".save-sync-extract-stage-*"))


def test_historical_extract_mid_write_failure_retains_only_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import grim_dawn_sync.git_vault as module
    destination = tmp_path / "destination"; vault = GitVault(tmp_path, runner=atomic_archive_runner(tmp_path))
    monkeypatch.setattr(module.shutil, "copyfileobj", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(SyncError) as caught: vault.extract_save("a" * 40, destination, machine_id="a", validator=valid)
    assert caught.value.code == "historical_extract_failed" and caught.value.exit_code == 3
    assert not destination.exists() and len(retained_extract_stages(tmp_path)) == 1


def test_historical_extract_validator_failure_retains_only_stage(tmp_path: Path) -> None:
    destination = tmp_path / "destination"; vault = GitVault(tmp_path, runner=atomic_archive_runner(tmp_path))
    invalid = lambda *_: {"ok": False, "classification": "invalid_save"}
    with pytest.raises(SyncError) as caught: vault.extract_save("a" * 40, destination, machine_id="a", validator=invalid)
    assert caught.value.code == "invalid_save" and caught.value.exit_code == 3
    assert not destination.exists() and len(retained_extract_stages(tmp_path)) == 1


def test_historical_extract_normalizes_validator_oserror(tmp_path: Path) -> None:
    destination = tmp_path / "destination"; vault = GitVault(tmp_path, runner=atomic_archive_runner(tmp_path))
    def disk_failure(*_):
        raise OSError("validation disk failure")
    with pytest.raises(SyncError) as caught: vault.extract_save("a" * 40, destination, machine_id="a", validator=disk_failure)
    assert caught.value.code == "historical_extract_validation_failed" and caught.value.exit_code == 3
    assert not destination.exists() and len(retained_extract_stages(tmp_path)) == 1


def test_historical_extract_rename_failure_retains_only_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import grim_dawn_sync.git_vault as module
    destination = tmp_path / "destination"; vault = GitVault(tmp_path, runner=atomic_archive_runner(tmp_path))
    original = module.os.rename
    def fail_stage(source: Path, target: Path) -> None:
        if Path(source).name.startswith(".save-sync-extract-stage-"): raise OSError("publish")
        original(source, target)
    monkeypatch.setattr(module.os, "rename", fail_stage)
    with pytest.raises(SyncError) as caught: vault.extract_save("a" * 40, destination, machine_id="a", validator=valid)
    assert caught.value.code == "historical_extract_publish_failed" and caught.value.exit_code == 3
    assert not destination.exists() and len(retained_extract_stages(tmp_path)) == 1


def test_historical_extract_atomically_publishes_validated_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import grim_dawn_sync.git_vault as module
    destination = tmp_path / "destination"; vault = GitVault(tmp_path, runner=atomic_archive_runner(tmp_path))
    original = module.os.rename; events: list[tuple[str, str]] = []; validated: list[Path] = []
    def record(source: Path, target: Path) -> None:
        events.append((Path(source).name, Path(target).name)); original(source, target)
    def validate_stage(root: Path, _manifest: dict) -> dict:
        validated.append(root); assert root.name.startswith(".save-sync-extract-stage-") and not destination.exists()
        return {"ok": True}
    monkeypatch.setattr(module.os, "rename", record)
    assert vault.extract_save("a" * 40, destination, machine_id="a", validator=validate_stage) == destination
    assert validated and events == [(validated[0].name, "destination")]
    assert (destination / "main/a/player.gdc").read_bytes() == b"atomic"
    assert not retained_extract_stages(tmp_path)


@pytest.mark.parametrize("change", ["hash", "extra", "missing"])
def test_validate_commit_snapshot_rejects_each_manifest_tree_difference(tmp_path: Path, change: str) -> None:
    _, clone, _ = clone_pair(tmp_path)
    vault = GitVault(clone)
    vault.snapshot(save(tmp_path / "source", b"original"), machine_id="a", session_id="base", validator=valid)
    player = clone / "save/main/a/player.gdc"
    if change == "hash":
        player.write_bytes(b"tampered")
        git(clone, "add", "save/main/a/player.gdc")
    elif change == "extra":
        extra = clone / "save/main/a/extra.gdc"; extra.write_bytes(b"extra")
        git(clone, "add", "save/main/a/extra.gdc")
    else:
        player.unlink()
        git(clone, "add", "-u", "save")
    git(clone, "commit", "-m", f"tamper-{change}")
    with pytest.raises(SyncError) as caught:
        vault.validate_commit_snapshot(vault._oid("HEAD") or "")
    assert caught.value.code == "committed_save_mismatch"


def test_validate_commit_snapshot_is_strictly_read_only(tmp_path: Path) -> None:
    _, clone, _ = clone_pair(tmp_path); vault = GitVault(clone)
    commit = vault.snapshot(save(tmp_path / "source"), machine_id="a", session_id="base", validator=valid)
    before = {path.relative_to(clone).as_posix() for path in clone.rglob("*")}
    assert vault.validate_commit_snapshot(commit)["root_hash"]
    assert before == {path.relative_to(clone).as_posix() for path in clone.rglob("*")}


def test_snapshot_refuses_source_changed_since_caller_manifest(tmp_path: Path) -> None:
    _, clone, _ = clone_pair(tmp_path); source = save(tmp_path / "source", b"before")
    expected = stable_manifest(source, machine_id="a", retries=1)
    save(source, b"after")
    with pytest.raises(SyncError) as caught:
        GitVault(clone).snapshot(source, machine_id="a", session_id="changed", expected_manifest=expected, validator=valid)
    assert caught.value.code == "source_changed" and not (clone / "save").exists()


def test_extract_manifest_mismatch_removes_unpublished_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import grim_dawn_sync.git_vault as module
    _, clone, _ = clone_pair(tmp_path); vault = GitVault(clone)
    commit = vault.snapshot(save(tmp_path / "source", b"original"), machine_id="a", session_id="base", validator=valid)
    real_manifest = module.stable_manifest
    def mismatch(root: Path, *args, **kwargs):
        result = real_manifest(root, *args, **kwargs)
        if root.name.startswith(".save-sync-extract-stage-"):
            result = dict(result); result["root_hash"] = "0" * 64
        return result
    monkeypatch.setattr(module, "stable_manifest", mismatch)
    destination = tmp_path / "destination"
    with pytest.raises(SyncError) as caught:
        vault.extract_save(commit, destination, machine_id="a", validator=valid)
    assert caught.value.code == "historical_extract_mismatch"
    assert not destination.exists() and not retained_extract_stages(tmp_path)
