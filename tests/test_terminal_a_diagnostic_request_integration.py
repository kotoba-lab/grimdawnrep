from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
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
    *, sequence: int = 4, expired: bool = False, bad_schema: bool = False
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
    # The checked-in request is sequence 4.  Seed the clone with the older
    # request so the test proves the canonical remote update is accepted.
    initial_payload = _request_payload(sequence=3)
    initial_payload["request_id"] = "00000000-0000-0000-0000-000000000001"
    _write_request(seed, initial_payload)
    _git(seed, "add", "ops/handoff/terminal-a-diagnostic-request.v1.json")
    _git(seed, "commit", "-m", "initial request")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "master")
    _run(REAL_GIT, "clone", str(remote), str(terminal))

    updated_payload = _request_payload(sequence=4, expired=expired, bad_schema=bad_schema)
    assert initial_payload["sequence"] == 3 < updated_payload["sequence"] == 4
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
        '{"sentinel":"TERMINAL_A_DIAGNOSIS","status":"blocked","leg":"A1",'
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
