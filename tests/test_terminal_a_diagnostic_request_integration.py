from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "operations" / "terminal-a-roundtrip-diagnose.md"
REQUEST = ROOT / "ops" / "handoff" / "terminal-a-diagnostic-request.v1.json"
PUBLIC_URL = "https://github.com/kotoba-lab/grimdawnrep.git"
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
REAL_GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not POWERSHELL.exists() or REAL_GIT is None,
    reason="the operator block is specifically for Windows PowerShell 5.1",
)


def _run(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=input,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _git(cwd: Path, *args: str) -> str:
    assert REAL_GIT is not None
    return _run(REAL_GIT, "-C", str(cwd), *args).stdout.strip()


def _request_payload(
    *, sequence: int = 6, expired: bool = False, bad_schema: bool = False
) -> dict[str, object]:
    payload = json.loads(REQUEST.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    not_before = now - (timedelta(hours=2) if expired else timedelta(seconds=30))
    expires = not_before + timedelta(hours=1)
    stamp = lambda value: value.strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["issued_at"] = stamp(not_before)
    payload["not_before"] = stamp(not_before)
    payload["expires_at"] = stamp(expires)
    payload["sequence"] = sequence
    if bad_schema:
        payload["schema_version"] = "2.0.0"
    return payload


def _write_request(repo: Path, payload: dict[str, object]) -> None:
    path = repo / "ops" / "handoff" / "terminal-a-diagnostic-request.v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="ascii", newline="\n")


def _make_git_wrapper(directory: Path) -> Path:
    assert REAL_GIT is not None
    wrapper = directory / "git.cmd"
    wrapper.write_text(
        "@echo off\r\n"
        "if \"%~3\"==\"remote\" if \"%~4\"==\"get-url\" if \"%~5\"==\"--all\" (\r\n"
        "  echo %GIT_MOCK_FETCH_URL%\r\n"
        "  if \"%GIT_MOCK_EXTRA_URL%\"==\"1\" echo https://example.invalid/extra.git\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        "if \"%~3\"==\"remote\" if \"%~4\"==\"get-url\" if \"%~5\"==\"--push\" (\r\n"
        "  echo %GIT_MOCK_PUSH_URL%\r\n"
        "  if \"%GIT_MOCK_EXTRA_URL%\"==\"1\" echo https://example.invalid/extra.git\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        "if \"%~3\"==\"rev-parse\" if \"%~4\"==\"FETCH_HEAD\" if not \"%GIT_MOCK_FETCH_HEAD%\"==\"\" (\r\n"
        "  echo %GIT_MOCK_FETCH_HEAD%\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        "if \"%~3\"==\"show\" if \"%GIT_MOCK_MUTATE_CONFIG%\"==\"1\" (\r\n"
        f'  "{REAL_GIT}" %*\r\n'
        "  echo changed>>\"%GIT_MOCK_CONFIG_PATH%\"\r\n"
        "  exit /b 0\r\n"
        ")\r\n"
        f'"{REAL_GIT}" %*\r\n'
        "exit /b %errorlevel%\r\n",
        encoding="ascii",
        newline="",
    )
    return wrapper


def _setup_case(
    base: Path,
    *,
    expired: bool = False,
    bad_schema: bool = False,
    missing_blob: bool = False,
    timestamp_fault: str | None = None,
) -> tuple[Path, Path, dict[str, str], str, str]:
    remote = base / "remote.git"
    seed = base / "seed"
    profile = base / "profile"
    terminal = profile / "grimdawnrep"
    local_app_data = base / "localappdata"
    wrapper_dir = base / "bin"
    wrapper_dir.mkdir()

    assert REAL_GIT is not None
    _run(REAL_GIT, "init", "--bare", str(remote))
    _run(REAL_GIT, "init", "-b", "master", str(seed))
    _git(seed, "config", "user.name", "Test Operator")
    _git(seed, "config", "user.email", "operator@example.invalid")
    # The checked-in request is sequence 6.  Seed the clone with the older
    # request so the test proves the canonical remote update is accepted.
    initial_payload = _request_payload(sequence=5)
    initial_payload["request_id"] = "00000000-0000-0000-0000-000000000001"
    _write_request(seed, initial_payload)
    _git(seed, "add", "ops/handoff/terminal-a-diagnostic-request.v1.json")
    _git(seed, "commit", "-m", "initial request")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "master")
    _run(REAL_GIT, "clone", str(remote), str(terminal))

    updated_payload = _request_payload(sequence=6, expired=expired, bad_schema=bad_schema)
    assert initial_payload["sequence"] == 5 < updated_payload["sequence"] == 6
    if timestamp_fault == "malformed":
        updated_payload["issued_at"] = "2026-08-01T09:46:11+00:00"
    _write_request(seed, updated_payload)
    if timestamp_fault == "duplicate":
        request_path = seed / "ops" / "handoff" / "terminal-a-diagnostic-request.v1.json"
        request_path.write_text(
            request_path.read_text(encoding="ascii").replace(
                '  "issued_at":', '  "issued_at": "2026-08-01T09:46:11Z",\n  "issued_at":', 1
            ),
            encoding="ascii",
            newline="\n",
        )
    if missing_blob:
        (seed / "ops" / "handoff" / "terminal-a-diagnostic-request.v1.json").unlink()
    _git(seed, "add", "ops/handoff/terminal-a-diagnostic-request.v1.json")
    _git(seed, "commit", "-m", "new request")
    new_commit = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "origin", "master")

    _git(terminal, "remote", "set-url", "origin", PUBLIC_URL)
    _git(terminal, "config", f"url.{remote.as_uri()}.insteadOf", PUBLIC_URL)
    before_head = _git(terminal, "rev-parse", "HEAD")

    python = local_app_data / "GrimDawnSaveSyncTool" / ".venv" / "Scripts" / "python.exe"
    config = local_app_data / "GrimDawnSaveSync" / "config.local.json"
    python.parent.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    python.write_bytes(b"deployed-python-fingerprint\n")
    config.write_bytes(b'{"machine_id":"desktop-a"}\n')
    _make_git_wrapper(wrapper_dir)

    env = os.environ.copy()
    env.update(
        USERPROFILE=str(profile),
        LOCALAPPDATA=str(local_app_data),
        PATH=str(wrapper_dir) + os.pathsep + env["PATH"],
        GIT_MOCK_FETCH_URL=PUBLIC_URL,
        GIT_MOCK_PUSH_URL=PUBLIC_URL,
        GIT_MOCK_EXTRA_URL="0",
        GIT_MOCK_FETCH_HEAD="",
        GIT_MOCK_MUTATE_CONFIG="0",
        GIT_MOCK_CONFIG_PATH=str(config),
    )
    return terminal, remote, env, before_head, new_commit


def _operator_script(base: Path) -> Path:
    section = RUNBOOK.read_text(encoding="utf-8").split("## Operator-mediated public remote request", 1)[1]
    command = section.split("```powershell", 1)[1].split("```", 1)[0]
    path = base / "operator-request.ps1"
    path.write_text(command, encoding="utf-8-sig", newline="\n")
    return path


def _classification_script(base: Path) -> Path:
    section = RUNBOOK.read_text(encoding="utf-8").split("## Copy-paste execution", 1)[1]
    command = section.split("```powershell", 1)[1].split("```", 1)[0]
    path = base / "classification.ps1"
    path.write_text(command, encoding="utf-8-sig", newline="\n")
    return path


def _invoke(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _assert_blocked(result: subprocess.CompletedProcess[str], stage: str, code: str) -> None:
    assert result.returncode == 1
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        '{"sentinel":"TERMINAL_A_REMOTE_CLASSIFICATION","status":"blocked","leg":"A1",'
        f'"machine_id":"desktop-a","stage":"{stage}","code":"{code}"}}'
    ]
    for secret in (
        PUBLIC_URL,
        "schema_version",
        "request_id",
        "ops/handoff",
        "refs/heads",
        "origin/master",
        "remote_request_invalid",
    ):
        assert secret not in result.stdout


def test_remote_request_block_fetches_explicit_destination_without_changing_head_or_worktree() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-request-") as raw_base:
        base = Path(raw_base)
        terminal, _remote, env, before_head, new_commit = _setup_case(base)
        script = _operator_script(base)

        result = _invoke(script, env)

        assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
        assert _git(terminal, "rev-parse", "HEAD") == before_head
        assert _git(terminal, "symbolic-ref", "HEAD") == "refs/heads/master"
        assert _git(terminal, "status", "--porcelain=v1", "--untracked-files=all") == ""
        assert _git(terminal, "rev-parse", "origin/master") == new_commit


@pytest.mark.parametrize(
    ("fault", "stage", "code"),
    [
        ("url_mismatch", "origin_identity", "origin_identity_invalid"),
        ("extra_url", "origin_identity", "origin_identity_invalid"),
        ("wrong_branch", "source_branch", "source_branch_invalid"),
        ("dirty", "source_clean", "source_clean_invalid"),
        ("fingerprint", "fingerprint", "fingerprint_invalid"),
        ("fetch_fail", "fetch", "fetch_failed"),
        ("non_ff", "fetch", "fetch_failed"),
        ("oid_mismatch", "oid", "oid_invalid"),
        ("ancestor", "ancestor", "ancestor_invalid"),
        ("blob", "blob", "blob_invalid"),
        ("schema", "schema", "schema_invalid"),
        ("expiry", "time", "time_invalid"),
        ("timestamp_malformed", "time", "time_invalid"),
        ("timestamp_duplicate", "time", "time_invalid"),
        ("post_invariant", "post_invariant", "post_invariant_invalid"),
    ],
)
def test_remote_request_block_fault_matrix_is_fail_closed_and_sanitized(
    fault: str, stage: str, code: str
) -> None:
    with tempfile.TemporaryDirectory(prefix=f"terminal-a-{fault}-") as raw_base:
        base = Path(raw_base)
        terminal, remote, env, _before_head, _new_commit = _setup_case(
            base,
            expired=fault == "expiry",
            bad_schema=fault == "schema",
            missing_blob=fault == "blob",
            timestamp_fault={"timestamp_malformed": "malformed", "timestamp_duplicate": "duplicate"}.get(fault),
        )
        script = _operator_script(base)

        if fault == "url_mismatch":
            env["GIT_MOCK_FETCH_URL"] = "https://example.invalid/wrong.git"
        elif fault == "extra_url":
            env["GIT_MOCK_EXTRA_URL"] = "1"
        elif fault == "dirty":
            (terminal / "untracked.txt").write_text("dirty\n", encoding="ascii")
        elif fault == "wrong_branch":
            _git(terminal, "checkout", "-b", "other")
        elif fault == "fingerprint":
            Path(env["LOCALAPPDATA"], "GrimDawnSaveSyncTool", ".venv", "Scripts", "python.exe").unlink()
        elif fault == "fetch_fail":
            remote.rename(base / "remote-unavailable.git")
        elif fault == "non_ff":
            tree = _git(terminal, "rev-parse", "HEAD^{tree}")
            divergent = _run(
                REAL_GIT or "git",
                "-C",
                str(terminal),
                "commit-tree",
                tree,
                input="divergent\n",
                env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                     "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"},
            ).stdout.strip()
            _git(terminal, "update-ref", "refs/remotes/origin/master", divergent)
        elif fault == "oid_mismatch":
            env["GIT_MOCK_FETCH_HEAD"] = "0000000000000000000000000000000000000001"
        elif fault == "ancestor":
            tree = _git(terminal, "rev-parse", "HEAD^{tree}")
            divergent = _run(
                REAL_GIT or "git",
                "-C",
                str(terminal),
                "commit-tree",
                tree,
                input="divergent-head\n",
                env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                     "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"},
            ).stdout.strip()
            _git(terminal, "update-ref", "refs/heads/master", divergent)
        elif fault == "post_invariant":
            env["GIT_MOCK_MUTATE_CONFIG"] = "1"

        _assert_blocked(_invoke(script, env), stage, code)


def test_classification_block_runs_in_ps51_and_fails_closed_without_leaking_paths() -> None:
    """The deployed classification block is executable and emits only its sentinel on bad input."""
    with tempfile.TemporaryDirectory(prefix="terminal-a-classification-") as raw_base:
        base = Path(raw_base)
        profile = base / "profile"
        local_app_data = base / "localappdata"
        (profile / "grimdawnrep").mkdir(parents=True)
        (profile / "Desktop").mkdir()
        python = local_app_data / "GrimDawnSaveSyncTool" / ".venv" / "Scripts" / "python.exe"
        config = local_app_data / "GrimDawnSaveSync" / "config.local.json"
        python.parent.mkdir(parents=True); config.parent.mkdir(parents=True)
        python.write_bytes(b"not-an-executable")
        config.write_text('{"machine_id":"desktop-a","vault_repo":"X"}', encoding="ascii")
        env = os.environ.copy()
        env.update(USERPROFILE=str(profile), LOCALAPPDATA=str(local_app_data))

        result = _invoke(_classification_script(base), env)

        assert result.returncode == 1 and result.stderr == ""
        assert result.stdout.splitlines() == [
            '{"sentinel":"TERMINAL_A_REMOTE_CLASSIFICATION","status":"blocked","leg":"A1",'
            '"machine_id":"desktop-a","classification":"unknown","content":"unknown","code":"observation_changed"}'
        ]
        for secret in (str(base), "vault_repo", "config.local", "not-an-executable"):
            assert secret not in result.stdout


def test_classification_block_uses_named_commandargs_for_git_helpers() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-static-") as raw_base:
        command = _classification_script(Path(raw_base)).read_text(encoding="utf-8-sig")
        assert "Invoke-GitLines -Repo $Repo -CommandArgs $CommandArgs" in command
        assert "Get-OneGitLine -Repo $source -CommandArgs @(" in command
        assert "Invoke-GitQuiet -Repo $vault -CommandArgs @(" in command
        assert "Get-Json -CommandArgs @('status')" in command
        assert "'origin',$remoteHead)" in command
        assert "'origin','refs/heads/main')" not in command
        assert not re.search(
            r"(?:Invoke-GitLines|Get-OneGitLine|Invoke-GitQuiet|Get-Json)\s+(?:\$[A-Za-z]+\s+)?@\(", command
        )


def _commit_manifest(repo: Path, root_hash: str, message: str) -> str:
    manifest = repo / ".sync" / "manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"root_hash": root_hash}) + "\n", encoding="ascii")
    _git(repo, "add", ".sync/manifest.json")
    # A same-content advance deliberately preserves the manifest blob.  The
    # ancestry exercise still needs a distinct commit, so make that explicit.
    _git(repo, "commit", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _classification_normal_case(
    base: Path, relation: str, content: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Create an isolated deployed layout for the normal classification matrix."""
    assert REAL_GIT is not None
    profile = base / "profile"
    source = profile / "grimdawnrep"
    desktop = profile / "Desktop"
    local_app_data = base / "localappdata"
    remote = base / "vault-remote.git"
    seed = base / "vault-seed"
    vault = base / "vault"
    live = base / "live-save"
    stub = base / "stub"
    desktop.mkdir(parents=True)
    live.mkdir()
    (live / "marker.txt").write_text("live-save-must-not-change\n", encoding="ascii")
    live_root = live / "root.txt"

    _run(REAL_GIT, "init", "--bare", str(remote))
    _run(REAL_GIT, "init", "-b", "main", str(seed))
    _git(seed, "config", "user.name", "Test Operator")
    _git(seed, "config", "user.email", "operator@example.invalid")
    base_root = "a" * 64
    _commit_manifest(seed, base_root, "base")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    base_commit = _git(seed, "rev-parse", "HEAD")
    _run(REAL_GIT, "clone", str(remote), str(vault))
    _git(vault, "checkout", "main")

    # Make exactly the requested ancestry relation without relying on the
    # production package's reconciliation implementation.
    remote_root = base_root
    if relation in {"remote_ahead", "diverged"}:
        remote_root = base_root if content == "same" else "b" * 64
        _commit_manifest(seed, remote_root, "remote advance")
        _git(seed, "push", "origin", "main")
    if relation in {"remote_behind", "diverged"}:
        local_root = base_root if content == "same" else "c" * 64
        _commit_manifest(vault, local_root, "local advance")
    local_head = _git(vault, "rev-parse", "HEAD")
    live_root.write_text(base_root + "\n", encoding="ascii")

    _run(REAL_GIT, "init", "-b", "master", str(source))
    _git(source, "config", "user.name", "Test Operator")
    _git(source, "config", "user.email", "operator@example.invalid")
    (source / "README").write_text("source\n", encoding="ascii")
    _git(source, "add", "README")
    _git(source, "commit", "-m", "source")

    tool_root = local_app_data / "GrimDawnSaveSyncTool"
    python = tool_root / ".venv" / "Scripts" / "python.exe"
    config = local_app_data / "GrimDawnSaveSync" / "config.local.json"
    state = config.parent / "state.json"
    python.parent.mkdir(parents=True)
    config.parent.mkdir(parents=True)
    # The deployed-shaped interpreter needs its adjacent core runtime too.
    shutil.copy2(sys.executable, python)
    interpreter_dir = Path(sys.executable).parent
    for runtime in ("python3.dll", "python313.dll"):
        shutil.copy2(interpreter_dir / runtime, python.parent / runtime)
    config.write_text(
        json.dumps({"machine_id": "desktop-a", "vault_repo": str(vault)}) + "\n",
        encoding="utf-8",
    )
    state.write_text('{"state":"unchanged"}\n', encoding="ascii")
    (stub / "grim_dawn_sync").mkdir(parents=True)
    (stub / "grim_dawn_sync" / "__init__.py").write_text("", encoding="ascii")
    (stub / "grim_dawn_sync" / "__main__.py").write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "command = sys.argv[-1]\n"
        f"root = {base_root!r}\n"
        "live_root = os.environ.get('GIT_CLASSIFY_LIVE_ROOT')\n"
        "if live_root: root = Path(live_root).read_text(encoding='ascii').strip()\n"
        f"head = {local_head!r}\n"
        "if command == 'status':\n"
        "    value = {'schema_version':'1.0.0','command':'status','processes':{'complete':True,'status':'clear'},'active_lock':None,'recovery_phase':None,'last_pushed_commit':head}\n"
        "elif command == 'doctor':\n"
        "    value = {'schema_version':'1.0.0','command':'doctor','read_only':True,'machine_id':'desktop-a','checks':{'save_root':{'manifest':{'root_hash':root}}}}\n"
        "else: raise SystemExit(2)\n"
        "print(json.dumps(value))\n",
        encoding="ascii",
    )
    shortcut = desktop / "Grim Dawn (DPYes + Save Selection).lnk"
    shortcut_script = base / "shortcut.ps1"
    shortcut_script.write_text(
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($args[0]);"
        "$s.TargetPath=$args[1];$s.WorkingDirectory=$args[2];"
        "$s.Arguments=\"-m grim_dawn_sync.cli 'launch'\";$s.Save()",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(shortcut_script), str(shortcut), str(python), str(source / "src")],
        check=False, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    wrapper_dir = base / "classification-bin"
    wrapper_dir.mkdir()
    # This wrapper is intentionally test-only.  It injects a mutation after
    # the manifest read, precisely between the before and after observations.
    (wrapper_dir / "git.cmd").write_text(
        "@echo off\r\n"
        "if \"%~3\"==\"fetch\" if \"%GIT_CLASSIFY_FAIL_FETCH%\"==\"1\" exit /b 2\r\n"
        "if \"%~3\"==\"merge-base\" if \"%GIT_CLASSIFY_FAIL_GRAPH%\"==\"1\" exit /b 2\r\n"
        "if not \"%~3\"==\"show\" goto real\r\n"
        "if \"%GIT_CLASSIFY_HOOK%\"==\"\" goto real\r\n"
        f'\"{REAL_GIT}\" %*\r\n'
        "if \"%GIT_CLASSIFY_HOOK%\"==\"config\" echo changed>>\"%GIT_CLASSIFY_CONFIG%\"\r\n"
        "if \"%GIT_CLASSIFY_HOOK%\"==\"state\" echo changed>>\"%GIT_CLASSIFY_STATE%\"\r\n"
        "if \"%GIT_CLASSIFY_HOOK%\"==\"live\" echo bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb>\"%GIT_CLASSIFY_LIVE_ROOT%\"\r\n"
        "if \"%GIT_CLASSIFY_HOOK%\"==\"source_worktree\" echo changed>>\"%GIT_CLASSIFY_SOURCE%\\hook.txt\"\r\n"
        "if \"%GIT_CLASSIFY_HOOK%\"==\"vault_worktree\" echo changed>>\"%GIT_CLASSIFY_VAULT%\\hook.txt\"\r\n"
        "if \"%GIT_CLASSIFY_HOOK%\"==\"source_head\" call \"%GIT_CLASSIFY_GIT%\" -C \"%GIT_CLASSIFY_SOURCE%\" commit --allow-empty -m hook 1>nul 2>nul\r\n"
        "if \"%GIT_CLASSIFY_HOOK%\"==\"vault_head\" call \"%GIT_CLASSIFY_GIT%\" -C \"%GIT_CLASSIFY_VAULT%\" commit --allow-empty -m hook 1>nul 2>nul\r\n"
        "if \"%GIT_CLASSIFY_HOOK%\"==\"vault_tag\" call \"%GIT_CLASSIFY_GIT%\" -C \"%GIT_CLASSIFY_VAULT%\" tag hook-tag\r\n"
        "if \"%GIT_CLASSIFY_HOOK%\"==\"vault_remote_ref\" call \"%GIT_CLASSIFY_GIT%\" -C \"%GIT_CLASSIFY_VAULT%\" update-ref refs/remotes/origin/hook %GIT_CLASSIFY_LOCAL_HEAD%\r\n"
        "if \"%GIT_CLASSIFY_HOOK%\"==\"fetch_head_create\" echo 1111111111111111111111111111111111111111>\"%GIT_CLASSIFY_FETCH_HEAD%\"\r\n"
        "if \"%GIT_CLASSIFY_HOOK%\"==\"fetch_head_change\" echo 2222222222222222222222222222222222222222>\"%GIT_CLASSIFY_FETCH_HEAD%\"\r\n"
        "if \"%GIT_CLASSIFY_HOOK%\"==\"remote_main\" call \"%GIT_CLASSIFY_GIT%\" -C \"%GIT_CLASSIFY_SEED%\" commit --allow-empty -m hook 1>nul 2>nul\r\n"
        "if \"%GIT_CLASSIFY_HOOK%\"==\"remote_main\" call \"%GIT_CLASSIFY_GIT%\" -C \"%GIT_CLASSIFY_SEED%\" push origin main 1>nul 2>nul\r\n"
        "if \"%GIT_CLASSIFY_HOOK%\"==\"remote_lock\" call \"%GIT_CLASSIFY_GIT%\" -C \"%GIT_CLASSIFY_SEED%\" tag -a grim-dawn-sync-active -m hook\r\n"
        "if \"%GIT_CLASSIFY_HOOK%\"==\"remote_lock\" call \"%GIT_CLASSIFY_GIT%\" -C \"%GIT_CLASSIFY_SEED%\" push origin refs/tags/grim-dawn-sync-active 1>nul 2>nul\r\n"
        "exit /b 0\r\n"
        ":real\r\n"
        f'\"{REAL_GIT}\" %*\r\n'
        "exit /b %errorlevel%\r\n",
        encoding="ascii", newline="",
    )
    env = os.environ.copy()
    env.update(
        USERPROFILE=str(profile),
        LOCALAPPDATA=str(local_app_data),
        PYTHONPATH=str(stub),
        PYTHONHOME=str(interpreter_dir),
        PATH=str(wrapper_dir) + os.pathsep + os.environ["PATH"],
        GIT_CLASSIFY_HOOK="",
        GIT_CLASSIFY_FAIL_FETCH="0",
        GIT_CLASSIFY_FAIL_GRAPH="0",
        GIT_CLASSIFY_CONFIG=str(config),
        GIT_CLASSIFY_STATE=str(state),
        GIT_CLASSIFY_LIVE_ROOT=str(live_root),
        GIT_CLASSIFY_SOURCE=str(source),
        GIT_CLASSIFY_VAULT=str(vault),
        GIT_CLASSIFY_SEED=str(seed),
        GIT_CLASSIFY_GIT=str(REAL_GIT),
        GIT_CLASSIFY_LOCAL_HEAD=local_head,
        GIT_CLASSIFY_FETCH_HEAD=str(vault / ".git" / "FETCH_HEAD"),
    )
    deployed_probe = subprocess.run(
        [str(python), "-m", "grim_dawn_sync", "--config", str(config), "--json", "status"],
        env=env, check=False, capture_output=True, text=True, encoding="utf-8",
    )
    assert deployed_probe.returncode == 0, deployed_probe.stderr
    paths = {"source": str(source), "vault": str(vault), "config": str(config), "state": str(state), "live": str(live), "live_root": str(live_root)}
    return env, paths


@pytest.mark.parametrize(
    ("relation", "content", "expected_status", "expected_code"),
    [
        ("equal", "same", "blocked", "not_target_relation"),
        ("remote_ahead", "same", "complete", "safe_remote_ahead"),
        ("remote_ahead", "different", "blocked", "remote_content_differs"),
        ("remote_behind", "same", "blocked", "not_target_relation"),
        ("diverged", "same", "blocked", "not_target_relation"),
    ],
)
def test_classification_block_normal_relation_matrix_is_read_only(
    relation: str, content: str, expected_status: str, expected_code: str
) -> None:
    with tempfile.TemporaryDirectory(prefix=f"terminal-a-normal-{relation}-") as raw_base:
        base = Path(raw_base)
        env, paths = _classification_normal_case(base, relation, content)
        vault = Path(paths["vault"])
        before = {
            "config": Path(paths["config"]).read_bytes(),
            "state": Path(paths["state"]).read_bytes(),
            "live": (Path(paths["live"]) / "marker.txt").read_bytes(),
            "worktree": _git(vault, "status", "--porcelain=v1", "--untracked-files=all"),
            "refs": _git(vault, "for-each-ref", "--format=%(refname) %(objectname)"),
        }

        result = _invoke(_classification_script(base), env)

        assert result.returncode == (0 if expected_status == "complete" else 1)
        assert result.stderr == ""
        assert result.stdout.splitlines() == [
            '{"sentinel":"TERMINAL_A_REMOTE_CLASSIFICATION","status":"'
            f'{expected_status}","leg":"A1","machine_id":"desktop-a","classification":"{relation}",'
            f'"content":"{content}","code":"{expected_code}"}}'
        ]
        assert Path(paths["config"]).read_bytes() == before["config"]
        assert Path(paths["state"]).read_bytes() == before["state"]
        assert (Path(paths["live"]) / "marker.txt").read_bytes() == before["live"]
        assert _git(vault, "status", "--porcelain=v1", "--untracked-files=all") == before["worktree"]
        assert _git(vault, "for-each-ref", "--format=%(refname) %(objectname)") == before["refs"]


@pytest.mark.parametrize("fault", ["fetch", "graph"])
def test_classification_block_fails_closed_on_advertised_fetch_or_graph_failure(fault: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f"terminal-a-classify-{fault}-") as raw_base:
        base = Path(raw_base)
        env, _paths = _classification_normal_case(base, "remote_ahead", "same")
        env[f"GIT_CLASSIFY_FAIL_{fault.upper()}"] = "1"

        result = _invoke(_classification_script(base), env)

        assert result.returncode == 1 and result.stderr == ""
        assert result.stdout.splitlines() == [
            '{"sentinel":"TERMINAL_A_REMOTE_CLASSIFICATION","status":"blocked","leg":"A1",'
            '"machine_id":"desktop-a","classification":"unknown","content":"unknown","code":"observation_changed"}'
        ]


@pytest.mark.parametrize(
    "mutation",
    [
        "remote_main", "remote_lock", "config", "state", "live",
        "source_head", "source_worktree", "vault_head", "vault_tag", "vault_worktree",
        "vault_remote_ref", "fetch_head_create", "fetch_head_change",
    ],
)
def test_classification_block_fails_closed_when_observed_state_changes(mutation: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f"terminal-a-classify-race-{mutation}-") as raw_base:
        base = Path(raw_base)
        env, _paths = _classification_normal_case(base, "remote_ahead", "same")
        fetch_head = Path(env["GIT_CLASSIFY_FETCH_HEAD"])
        if mutation == "fetch_head_create":
            fetch_head.unlink(missing_ok=True)
        elif mutation == "fetch_head_change":
            fetch_head.write_text("0000000000000000000000000000000000000000\n", encoding="ascii")
        env["GIT_CLASSIFY_HOOK"] = mutation

        result = _invoke(_classification_script(base), env)

        assert result.returncode == 1 and result.stderr == ""
        assert result.stdout.splitlines() == [
            '{"sentinel":"TERMINAL_A_REMOTE_CLASSIFICATION","status":"blocked","leg":"A1",'
            '"machine_id":"desktop-a","classification":"unknown","content":"unknown","code":"observation_changed"}'
        ]
        for secret in (str(base), PUBLIC_URL, "hook", "refs/heads", "root_hash"):
            assert secret not in result.stdout
