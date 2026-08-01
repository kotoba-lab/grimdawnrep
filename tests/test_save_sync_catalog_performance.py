from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from grim_dawn_sync.bookmarks import _create_bookmark_test_only
from grim_dawn_sync.errors import SyncError
from grim_dawn_sync.git_vault import GitResult, GitRunner, GitVault
from grim_dawn_sync.version_catalog import VersionCatalogBuilder
from test_save_sync_git_vault import clone_pair, git, save, valid


@pytest.mark.parametrize("method", ["run", "run_bytes", "run_bytes_input"])
def test_git_runner_is_noninteractive_and_maps_timeout_without_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    seen: dict[str, object] = {}

    class TimedOutProcess:
        pid = 9182
        returncode = None

        def communicate(self, *_args, **kwargs):
            raise subprocess.TimeoutExpired("git", kwargs["timeout"])

        def kill(self) -> None:
            seen["killed"] = True

    def timeout(argv, **kwargs):
        seen.update(kwargs)
        seen["argv"] = argv
        return TimedOutProcess()

    def taskkill(argv, **kwargs):
        seen["taskkill"] = (argv, kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setenv("GIT_ASKPASS", "unsafe-prompt.exe")
    monkeypatch.setenv("SSH_ASKPASS", "unsafe-ssh-prompt.exe")
    monkeypatch.setenv("SSH_AUTH_SOCK", "key-agent.sock")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "private-helper-config")
    monkeypatch.setattr("grim_dawn_sync.git_vault.subprocess.Popen", timeout)
    monkeypatch.setattr("grim_dawn_sync.git_vault.subprocess.run", taskkill)
    runner = GitRunner(tmp_path, timeout_seconds=1.25)
    with pytest.raises(SyncError) as caught:
        if method == "run":
            runner.run("status")
        elif method == "run_bytes":
            runner.run_bytes("ls-files", "-z")
        else:
            runner.run_bytes_input("hash-object", "--stdin", input_bytes=b"private")

    assert caught.value.code == "git_timeout"
    assert caught.value.details == {}
    assert seen["argv"] == ["git", "status"] or method != "run"
    assert seen["shell"] is False
    assert seen["stdin"] == subprocess.DEVNULL or method == "run_bytes_input"
    environment = seen["env"]
    assert isinstance(environment, dict)
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GCM_INTERACTIVE"] == "Never"
    assert environment["SSH_ASKPASS_REQUIRE"] == "never"
    assert environment["GIT_ASKPASS"] == "git"
    assert environment["SSH_ASKPASS"] == "git"
    assert environment["SSH_AUTH_SOCK"] == "key-agent.sock"
    assert environment["GIT_CONFIG_GLOBAL"] == "private-helper-config"
    assert "GIT_CONFIG_NOSYSTEM" not in environment
    if os.name == "nt":
        assert seen["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
        taskkill_argv, taskkill_options = seen["taskkill"]
        assert taskkill_argv == ["taskkill", "/PID", "9182", "/T", "/F"]
        assert taskkill_options["shell"] is False
    assert seen["killed"] is True


def test_catalog_validates_each_commit_and_shared_blob_once(tmp_path: Path) -> None:
    _, clone, _ = clone_pair(tmp_path)
    source = save(tmp_path / "source", b"shared")
    vault = GitVault(clone)
    first = vault.snapshot(source, machine_id="a", session_id="first", validator=valid)
    vault.push(first)
    second = vault.snapshot(source, machine_id="a", session_id="second", validator=valid)
    vault.push(second)
    _create_bookmark_test_only(vault, first, display_name="same commit", note=None, created_by="a")
    git(clone, "tag", "-a", "archive/same-commit", first, "-m", "legacy")

    vault.runner.commands.clear()
    catalog = VersionCatalogBuilder(vault, source, machine_id="a").build(history_limit=10)
    commands = [tuple(command[1:]) for command in vault.runner.commands]

    assert catalog.candidates
    for commit in (first, second):
        assert commands.count(("show", f"{commit}:.sync/manifest.json")) == 1
        assert commands.count(("show", f"{commit}:.sync/vault.json")) == 1
        assert commands.count(("ls-tree", "-r", "-z", commit, "--", "save")) == 1
    assert sum(command[:2] == ("cat-file", "--batch") for command in commands) == 1
    assert not any(command[:2] == ("cat-file", "blob") for command in commands)


@pytest.mark.parametrize("output", [
    b"",
    ("a" * 40 + " tree 1\nx\n").encode("ascii"),
    ("a" * 40 + " blob 2\nx\n").encode("ascii"),
    ("b" * 40 + " blob 1\nx\n").encode("ascii"),
    ("a" * 40 + " blob 1\nxx").encode("ascii"),
])
def test_cat_file_batch_parser_rejects_malformed_output_before_caching(
    tmp_path: Path, output: bytes,
) -> None:
    class Runner:
        def run_bytes_input(self, *_args: str, **_kwargs: object) -> bytes:
            return output

    cache: dict[str, tuple[int, str]] = {}
    with pytest.raises(SyncError) as caught:
        GitVault(tmp_path, runner=Runner())._read_blob_batch(("a" * 40,), cache)  # type: ignore[arg-type]
    assert caught.value.code == "invalid_remote_manifest" and cache == {}


@pytest.mark.parametrize("failure", ["malformed", "timeout"])
def test_cat_file_batch_cache_is_transactional_across_later_chunks(
    tmp_path: Path, failure: str,
) -> None:
    oids = tuple(f"{index:040x}" for index in range(1, 258))

    class Runner:
        calls = 0
        def run_bytes_input(self, *_args: str, **kwargs: object) -> bytes:
            self.calls += 1
            chunk = bytes(kwargs["input_bytes"]).decode("ascii").splitlines()
            if self.calls == 2:
                if failure == "timeout":
                    raise SyncError("git_timeout", "Git command timed out.", 4)
                return b"malformed\n"
            return b"".join(f"{oid} blob 1\n".encode("ascii") + b"x\n" for oid in chunk)

    original = {"f" * 40: (7, "e" * 64)}
    cache = dict(original)
    with pytest.raises(SyncError) as caught:
        GitVault(tmp_path, runner=Runner())._read_blob_batch(oids, cache)  # type: ignore[arg-type]
    assert caught.value.code == ("git_timeout" if failure == "timeout" else "invalid_remote_manifest")
    assert cache == original


def test_cat_file_fallback_cache_is_transactional_across_later_oid(tmp_path: Path) -> None:
    oids = ("a" * 40, "b" * 40, "c" * 40)

    class Runner:
        calls = 0
        def run_bytes(self, *_args: str) -> bytes:
            self.calls += 1
            if self.calls == 2:
                raise SyncError("git_timeout", "Git command timed out.", 4)
            return b"verified"

    original = {"f" * 40: (7, "e" * 64)}
    cache = dict(original)
    with pytest.raises(SyncError) as caught:
        GitVault(tmp_path, runner=Runner())._read_blob_batch(oids, cache)  # type: ignore[arg-type]
    assert caught.value.code == "git_timeout"
    assert cache == original


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree termination contract")
def test_windows_timeout_terminates_spawned_process_tree(tmp_path: Path) -> None:
    import ctypes

    script = tmp_path / "spawn_tree.py"
    pid_file = tmp_path / "child.pid"
    script.write_text(
        "import pathlib,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid),encoding='ascii')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    def alive(pid: int) -> bool:
        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(process)

    child_pid: int | None = None
    try:
        with pytest.raises(SyncError) as caught:
            GitRunner(tmp_path, executable=sys.executable, timeout_seconds=0.75).run(
                str(script), str(pid_file),
            )
        assert caught.value.code == "git_timeout" and caught.value.details == {}
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pid_file.exists()
        child_pid = int(pid_file.read_text(encoding="ascii"))
        while alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not alive(child_pid)
    finally:
        if child_pid is not None and alive(child_pid):
            subprocess.run(
                ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                check=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )


@pytest.mark.skipif(os.name != "nt", reason="Windows askpass process contract")
def test_real_git_keeps_noninteractive_credential_helper_and_blocks_askpass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    global_config = tmp_path / "global.gitconfig"
    credential_file = tmp_path / "credentials.txt"
    sentinel = tmp_path / "askpass.cmd"
    sentinel_log = tmp_path / "askpass.log"
    sentinel.write_text(
        f"@echo off\r\necho called>>\"{sentinel_log}\"\r\necho sentinel-value\r\n",
        encoding="utf-8",
    )
    credential_file.write_text("https://helper-user:helper-pass@example.invalid/\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["GIT_CONFIG_GLOBAL"] = str(global_config)
    environment["GIT_ASKPASS"] = str(sentinel)
    environment["SSH_ASKPASS"] = str(sentinel)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, env=environment)
    subprocess.run(
        ["git", "config", "--global", "core.askPass", str(sentinel)],
        check=True, capture_output=True, env=environment,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "credential.helper", f"store --file={credential_file.as_posix()}"],
        check=True, capture_output=True, env=environment,
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_ASKPASS", str(sentinel))
    monkeypatch.setenv("SSH_ASKPASS", str(sentinel))
    runner = GitRunner(repo, timeout_seconds=5)
    filled = runner.run(
        "credential", "fill",
        input_text="protocol=https\nhost=example.invalid\n\n",
    )
    assert "username=helper-user" in filled.stdout
    assert "password=helper-pass" in filled.stdout
    assert not sentinel_log.exists()

    subprocess.run(
        ["git", "-C", str(repo), "config", "--unset-all", "credential.helper"],
        check=True, capture_output=True, env=environment,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "--add", "credential.helper", ""],
        check=True, capture_output=True, env=environment,
    )
    with pytest.raises(SyncError) as caught:
        runner.run(
            "credential", "fill",
            input_text="protocol=https\nhost=no-credential.invalid\n\n",
        )
    assert caught.value.code == "git_command_failed"
    assert not sentinel_log.exists()


@pytest.mark.parametrize("oversized", ["count", "bytes"])
def test_legacy_remote_listing_fails_before_fetch(tmp_path: Path, oversized: str) -> None:
    if oversized == "count":
        lines: list[str] = []
        for index in range(101):
            ref = f"refs/tags/archive/{index:03d}"
            lines.extend((f"{'a' * 40}\t{ref}", f"{'b' * 40}\t{ref}^{{}}"))
        remote_output = "\n".join(lines) + "\n"
    else:
        remote_output = "x" * (128 * 1024 + 1)

    class Runner:
        def __init__(self) -> None:
            self.commands: list[tuple[str, ...]] = []

        def run(self, *args: str, **_kwargs: object) -> GitResult:
            self.commands.append(args)
            if args[0] == "for-each-ref":
                return GitResult(args, "", "", 0)
            if args[0] == "ls-remote":
                return GitResult(args, remote_output, "", 0)
            raise AssertionError(args)

    runner = Runner()
    with pytest.raises(SyncError) as caught:
        GitVault(tmp_path, runner=runner).legacy_annotated_tags()  # type: ignore[arg-type]
    assert caught.value.code == "legacy_tag_list_too_large"
    assert not any(command[0] == "fetch" for command in runner.commands)


def _managed_remote_output(rows: list[tuple[str, str, str]]) -> str:
    lines: list[str] = []
    for ref, tag_oid, target in rows:
        lines.extend((f"{tag_oid}\t{ref}", f"{target}\t{ref}^{{}}"))
    return "\n".join(lines) + "\n"


def test_managed_missing_tags_use_bounded_exact_no_force_batch_fetches(tmp_path: Path) -> None:
    rows = [
        (f"refs/tags/grim-dawn-save-{index:032x}", f"{index + 1:040x}", f"{index + 11:040x}")
        for index in range(100)
    ]

    class Runner:
        def __init__(self) -> None:
            self.commands: list[tuple[str, ...]] = []
            self.fetched = False

        def run(self, *args: str, **_kwargs: object) -> GitResult:
            self.commands.append(args)
            if args[0] == "ls-remote":
                return GitResult(args, _managed_remote_output(rows), "", 0)
            if args[0] == "fetch":
                self.fetched = True
                return GitResult(args, "", "", 0)
            if args[:3] == ("rev-parse", "--verify", "--quiet"):
                ref = args[3]
                base = ref[:-3] if ref.endswith("^{}") else ref
                expected = next(item for item in rows if item[0] == base)
                value = expected[2] if ref.endswith("^{}") else expected[1]
                return GitResult(args, value + "\n" if self.fetched else "", "", 0 if self.fetched else 1)
            if args[:2] == ("cat-file", "tag"):
                ref = args[2]
                annotation = json.dumps({
                    "schema_version": "1.0.0", "display_name": ref[-4:], "note": None, "created_by": "a",
                })
                return GitResult(args, f"object x\n\n{annotation}\n", "", 0)
            raise AssertionError(args)

    runner = Runner()
    result = GitVault(tmp_path, runner=runner).managed_bookmarks()  # type: ignore[arg-type]
    fetches = [command for command in runner.commands if command[0] == "fetch"]
    assert len(result) == 100 and len(fetches) == 7
    flattened = tuple(argument for command in fetches for argument in command[3:])
    assert flattened == tuple(f"{ref}:{ref}" for ref, _tag, _target in rows)
    assert all(command[:3] == ("fetch", "--no-tags", "origin") and len(command[3:]) <= 16
               for command in fetches)
    assert not any(argument.startswith("+") or argument == "--force"
                   for command in fetches for argument in command)


def test_tag_collision_is_rejected_before_any_batch_fetch(tmp_path: Path) -> None:
    rows = [
        (f"refs/tags/grim-dawn-save-{index:032x}", f"{index + 1:040x}", f"{index + 11:040x}")
        for index in range(2)
    ]

    class Runner:
        def __init__(self) -> None:
            self.commands: list[tuple[str, ...]] = []

        def run(self, *args: str, **_kwargs: object) -> GitResult:
            self.commands.append(args)
            if args[0] == "ls-remote":
                return GitResult(args, _managed_remote_output(rows), "", 0)
            if args[:3] == ("rev-parse", "--verify", "--quiet"):
                value = "" if args[3] == rows[0][0] else "f" * 40 + "\n"
                return GitResult(args, value, "", 0 if value else 1)
            raise AssertionError(args)

    runner = Runner()
    with pytest.raises(SyncError) as caught:
        GitVault(tmp_path, runner=runner).managed_bookmarks()  # type: ignore[arg-type]
    assert caught.value.code == "bookmark_local_collision"
    assert not any(command[0] == "fetch" for command in runner.commands)


@pytest.mark.parametrize(("kind", "oversized"), [
    ("legacy", "count"), ("legacy", "bytes"), ("legacy", "ref"),
    ("managed", "count"), ("managed", "bytes"), ("managed", "ref"),
])
def test_local_tag_enumeration_has_remote_equivalent_bounds(
    tmp_path: Path, kind: str, oversized: str,
) -> None:
    oid = "a" * 40
    if kind == "legacy":
        if oversized == "count":
            output = "".join(f"archive/{index:03d}\ttag\t{oid}\t{oid}\n" for index in range(101))
        elif oversized == "bytes":
            output = "x" * (128 * 1024 + 1)
        else:
            output = f"archive/{'x' * 505}\ttag\t{oid}\t{oid}\n"
    else:
        if oversized == "count":
            output = "".join(
                f"refs/tags/grim-dawn-save-{index:032x}\t{oid}\t{oid}\n" for index in range(101)
            )
        elif oversized == "bytes":
            output = "x" * (128 * 1024 + 1)
        else:
            output = f"refs/tags/grim-dawn-save-{'a' * 600}\t{oid}\t{oid}\n"

    class Runner:
        def run(self, *args: str, **_kwargs: object) -> GitResult:
            assert args[0] == "for-each-ref"
            return GitResult(args, output, "", 0)

    vault = GitVault(tmp_path, runner=Runner())  # type: ignore[arg-type]
    with pytest.raises(SyncError) as caught:
        if kind == "legacy":
            vault.legacy_annotated_tags()
        else:
            vault._managed_bookmark_rows(remote=False)
    if oversized in {"count", "bytes"}:
        assert caught.value.code == ("legacy_tag_list_too_large" if kind == "legacy" else "bookmark_list_too_large")
    else:
        assert caught.value.code == ("malformed_legacy_tag" if kind == "legacy" else "malformed_bookmark_ref")
