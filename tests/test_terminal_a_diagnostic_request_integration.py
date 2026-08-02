from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
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
    *, sequence: int = 13, expired: bool = False, bad_schema: bool = False
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
        "if \"%~3\"==\"show\" if \"%GIT_MOCK_MUTATE_SKILL%\"==\"1\" (\r\n"
        f'  "{REAL_GIT}" %*\r\n'
        "  echo changed>>\"%GIT_MOCK_SKILL_PATH%\"\r\n"
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
    # The checked-in request is sequence 13. Seed the clone with the older
    # request so the test proves the canonical remote update is accepted.
    initial_payload = _request_payload(sequence=12)
    initial_payload["request_id"] = "00000000-0000-0000-0000-000000000001"
    _write_request(seed, initial_payload)
    package = seed / "src" / "grim_dawn_sync"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("\n", encoding="ascii")
    (package / "__main__.py").write_text("\n", encoding="ascii")
    _git(seed, "add", "ops/handoff/terminal-a-diagnostic-request.v1.json", "src/grim_dawn_sync")
    _git(seed, "commit", "-m", "initial request")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "master")
    _run(REAL_GIT, "clone", str(remote), str(terminal))

    agents_skill = terminal / ".agents" / "skills" / "grim-dawn-buildcraft"
    claude_skill = terminal / ".claude" / "skills" / "grim-dawn-buildcraft"
    (agents_skill / "agents").mkdir(parents=True)
    (agents_skill / "references" / "empty").mkdir(parents=True)
    (claude_skill / "empty").mkdir(parents=True)
    (agents_skill / "SKILL.md").write_text("agent skill\n", encoding="utf-8")
    (agents_skill / "agents" / "openai.yaml").write_text("model: test\n", encoding="utf-8")
    (agents_skill / "references" / "extra.md").write_text("extra\n", encoding="utf-8")
    (claude_skill / "SKILL.md").write_text("claude skill\n", encoding="utf-8")

    updated_payload = _request_payload(sequence=13, expired=expired, bad_schema=bad_schema)
    assert initial_payload["sequence"] == 12 < updated_payload["sequence"] == 13
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
        GIT_MOCK_MUTATE_SKILL="0",
        GIT_MOCK_SKILL_PATH=str(agents_skill / "SKILL.md"),
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


def _summary_script(base: Path) -> Path:
    section = RUNBOOK.read_text(encoding="utf-8").split(
        "## Copy-paste execution: validated remote diff summary", 1
    )[1]
    command = section.split("```powershell", 1)[1].split("```", 1)[0]
    path = base / "remote-diff-summary.ps1"
    path.write_text(command, encoding="utf-8-sig", newline="\n")
    return path


def _post_failure_probe_script(base: Path) -> Path:
    section = RUNBOOK.read_text(encoding="utf-8").split("## Post-selector-failure readonly probe", 1)[1]
    command = section.split("```powershell", 1)[1].split("```", 1)[0]
    path = base / "post-selector-failure-probe.ps1"
    path.write_text(command, encoding="utf-8-sig", newline="\n")
    return path


def _stage_probe_script(base: Path) -> Path:
    section = RUNBOOK.read_text(encoding="utf-8").split("## Post-selector-failure stage probe (sequence 10)", 1)[1]
    command = section.split("```powershell", 1)[1].split("```", 1)[0]
    path = base / "stage-probe.ps1"
    path.write_text(command, encoding="utf-8-sig", newline="\n")
    return path


def _source_path_repair_script(base: Path) -> Path:
    section = RUNBOOK.read_text(encoding="utf-8").split("## Source-path runtime repair (sequence 13)", 1)[1]
    command = section.split("```powershell", 1)[1].split("```", 1)[0]
    path = base / "source-path-repair.ps1"
    path.write_text(command, encoding="utf-8-sig", newline="\n")
    return path


def _invoke(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        cwd=env.get("GIT_SHIM_DIR"),
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
        porcelain = _git(terminal, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
        assert porcelain and all(
            row.startswith("?? .agents/skills/grim-dawn-buildcraft/")
            or row.startswith("?? .claude/skills/grim-dawn-buildcraft/")
            for row in porcelain
        )
        assert _git(terminal, "rev-parse", "origin/master") == new_commit


@pytest.mark.parametrize(
    ("fault", "stage", "code"),
    [
        ("url_mismatch", "origin_identity", "origin_identity_invalid"),
        ("extra_url", "origin_identity", "origin_identity_invalid"),
        ("wrong_branch", "source_branch", "source_branch_invalid"),
        ("other_untracked", "source_policy", "source_policy_invalid"),
        ("tracked_dirty", "source_policy", "source_policy_invalid"),
        ("missing_skill_root", "source_policy", "source_policy_invalid"),
        ("intent_to_add_in_skill", "source_policy", "source_policy_invalid"),
        ("skill_root_reparse", "user_skills", "user_skills_invalid"),
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
        ("skill_changed", "post_invariant", "post_invariant_invalid"),
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
        elif fault == "other_untracked":
            (terminal / "untracked.txt").write_text("dirty\n", encoding="ascii")
        elif fault == "tracked_dirty":
            (terminal / "src" / "grim_dawn_sync" / "__init__.py").write_text("dirty\n", encoding="ascii")
        elif fault == "missing_skill_root":
            shutil.rmtree(terminal / ".claude" / "skills" / "grim-dawn-buildcraft")
        elif fault == "intent_to_add_in_skill":
            _git(terminal, "add", "-N", ".agents/skills/grim-dawn-buildcraft/SKILL.md")
        elif fault == "skill_root_reparse":
            skill_root = terminal / ".claude" / "skills" / "grim-dawn-buildcraft"
            outside = base / "reparse-target"
            shutil.rmtree(skill_root)
            outside.mkdir()
            (outside / "SKILL.md").write_text("outside\n", encoding="ascii")
            made = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(skill_root), str(outside)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            assert made.returncode == 0, made.stderr
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
        elif fault == "skill_changed":
            env["GIT_MOCK_MUTATE_SKILL"] = "1"

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


def _write_snapshot(repo: Path, files: dict[str, bytes], message: str) -> tuple[str, str]:
    """Commit a real, package-validated save snapshot for the summary block."""
    save = repo / "save"
    shutil.rmtree(save, ignore_errors=True)
    for name, value in files.items():
        path = save / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    rows = []
    for name in sorted(files, key=str.casefold):
        data = files[name]
        rows.append({"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    root = hashlib.sha256("\n".join(f"{row['path']}\0{row['size']}\0{row['sha256']}" for row in rows).encode()).hexdigest()
    manifest = {"schema_version":"1.0.0", "created_at":"2026-08-01T00:00:00+00:00", "machine_id":"desktop-a", "root_hash":root, "file_count":len(rows), "total_bytes":sum(row["size"] for row in rows), "character_count":sum(1 for row in rows if row["path"].casefold().count("/")==2 and row["path"].casefold().startswith("main/") and row["path"].casefold().endswith("/player.gdc")), "files":rows}
    sync = repo / ".sync"
    sync.mkdir(exist_ok=True)
    (sync / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    (sync / "vault.json").write_text(json.dumps({"schema_version": "1.0.0", "machine_id": "desktop-a", "session_id": "test", "root_hash": manifest["root_hash"]}) + "\n", encoding="utf-8")
    _git(repo, "add", "save", ".sync")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD"), str(manifest["root_hash"])


def _summary_case(base: Path, *, remote_files: dict[str, bytes], corrupt: str | None = None) -> tuple[dict[str, str], dict[str, str]]:
    assert REAL_GIT is not None
    profile, local = base / "profile", base / "localappdata"
    source, seed, vault, remote, live, stub = profile / "grimdawnrep", base / "seed", base / "vault", base / "remote.git", base / "live", base / "stub"
    _run(REAL_GIT, "init", "--bare", str(remote)); _run(REAL_GIT, "init", "-b", "main", str(seed))
    for repo in (seed,):
        _git(repo, "config", "user.name", "Test"); _git(repo, "config", "user.email", "test@example.invalid")
    baseline_files = {"main/Hero/player.gdc": b"base", "main/Hero/quests.gdd": b"quest", "transfer.gst": b"outside"}
    baseline, _baseline_root = _write_snapshot(seed, baseline_files, "baseline")
    _git(seed, "remote", "add", "origin", str(remote)); _git(seed, "push", "-u", "origin", "main")
    _run(REAL_GIT, "clone", str(remote), str(vault)); _git(vault, "checkout", "main")
    remote_head, remote_root = _write_snapshot(seed, remote_files, "remote"); _git(seed, "push", "origin", "main")
    if corrupt:
        if corrupt == "blob": (seed / "save" / "transfer.gst").write_bytes(b"tampered")
        elif corrupt == "tree": (seed / "save" / "extra.bin").write_bytes(b"extra")
        else:
            manifest = json.loads((seed / ".sync" / "manifest.json").read_text(encoding="utf-8")); manifest["root_hash"] = "0" * 64; (seed / ".sync" / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        _git(seed, "add", "save", ".sync"); _git(seed, "commit", "-m", "corrupt"); remote_head = _git(seed, "rev-parse", "HEAD"); _git(seed, "push", "origin", "main")
    _run(REAL_GIT, "init", "-b", "master", str(source)); _git(source, "config", "user.name", "Test"); _git(source, "config", "user.email", "test@example.invalid")
    (source / "README").write_text("source\n", encoding="ascii"); _git(source, "add", "README"); _git(source, "commit", "-m", "source")
    # The mock doctor reports the baseline snapshot.  Materialize that exact
    # snapshot at the live root too, so the post-failure probe's independent
    # installed-package manifest check exercises its success path rather than
    # relying on the mock doctor's assertion alone.
    live.mkdir()
    for name, value in baseline_files.items():
        target = live / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    # The probe independently recomputes the live root with the installed
    # package.  Keep the mock doctor's value identical to that real result;
    # the committed baseline manifest remains intentionally independent.
    source_path = str(ROOT / "src")
    sys.path.insert(0, source_path)
    try:
        from grim_dawn_sync.manifest import build_manifest
        live_root = str(build_manifest(live, machine_id="desktop-a")["root_hash"])
    finally:
        sys.path.remove(source_path)
    # Doctor's root is intentionally supplied by the fixed local test package;
    # no fetched source is imported.
    tool_python = local / "GrimDawnSaveSyncTool" / ".venv" / "Scripts" / "python.exe"; tool_python.parent.mkdir(parents=True); shutil.copy2(sys.executable, tool_python)
    interp = Path(sys.executable).parent
    for runtime in ("python3.dll", "python313.dll"):
        candidate = interp / runtime
        if candidate.exists(): shutil.copy2(candidate, tool_python.parent / runtime)
    config = local / "GrimDawnSaveSync" / "config.local.json"; config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"machine_id":"desktop-a", "vault_repo":str(vault), "save_root":str(live)}) + "\n", encoding="utf-8")
    state = config.parent / "state.json"; state.write_text("{}\n", encoding="ascii")
    package = stub / "grim_dawn_sync"; package.mkdir(parents=True)
    (package / "__init__.py").write_text("from pkgutil import extend_path\n__path__=extend_path(__path__,__name__)\n", encoding="ascii")
    (package / "__main__.py").write_text(
        "import json,os,subprocess,sys\nfrom pathlib import Path\ncmd=sys.argv[-1]\n"
        f"root={live_root!r}\nhead={baseline!r}\n"
        "hook=os.environ.get('GIT_PROBE_HOOK',''); fault=os.environ.get('STAGE_PROBE_FAULT',''); counter=os.environ.get('GIT_PROBE_COUNTER','')\n"
        "count=int(open(counter).read()) if counter and os.path.exists(counter) else 0\n"
        "if cmd in ('status','doctor') and counter: open(counter,'w').write(str(count+1))\n"
        "trigger=(bool(hook) or fault=='post_invariant_changed') and count==2 and cmd=='status'\n"
        "if (fault=='status_command_failed' and cmd=='status') or (fault=='doctor_command_failed' and cmd=='doctor'): raise SystemExit(2)\n"
        "if cmd=='status': print(json.dumps({} if fault=='status_shape_invalid' else {'schema_version':('2.0.0' if fault=='status_schema_drift' and count>=2 else '1.0.0'),'command':('doctor' if fault=='status_command_drift' and count>=2 else 'status'),'readiness':('ready' if fault=='status_not_stable' and count>=2 else 'blocked'),'vault_relation':'remote_changed_or_unknown','active_lock':None,'recovery_phase':None,'processes':{'complete':not(fault=='process_complete_drift' and count>=2),'status':'clear'},'volatile':count,'last_pushed_commit':head}))\n"
        "elif cmd=='doctor': print(json.dumps({} if fault=='doctor_shape_invalid' else {'schema_version':'1.0.0','command':'doctor','read_only':not(fault=='doctor_read_only_drift' and count>=3),'machine_id':('desktop-b' if fault=='doctor_not_stable' and count>=3 else 'desktop-a'),'passed':True,'volatile':count,'checks':{'save_root':{'manifest':{'root_hash':('0'*64 if fault=='installed_manifest_mismatch' or fault=='late_doctor_root' and count>=4 else root)}}}}))\n"
        "else: raise SystemExit(2)\n"
        "if trigger:\n"
        " git=os.environ['REAL_GIT']\n"
        " if hook=='config' or fault=='post_invariant_changed': open(os.environ['GIT_PROBE_CONFIG'],'a').write('x')\n"
        " elif hook=='state': open(os.environ['GIT_PROBE_STATE'],'a').write('x')\n"
        " elif hook=='live': open(os.path.join(os.environ['GIT_PROBE_LIVE'],'race.bin'),'wb').write(b'x')\n"
        " elif hook=='package_py': open(os.environ['GIT_PROBE_PACKAGE_MAIN'],'a').write('\\n# race')\n"
        " elif hook=='source_ref': subprocess.run([git,'-C',os.environ['GIT_PROBE_SOURCE'],'update-ref','refs/heads/probe',os.environ['GIT_PROBE_SOURCE_HEAD']],check=True)\n"
        " elif hook=='vault_ref': subprocess.run([git,'-C',os.environ['GIT_PROBE_VAULT'],'update-ref','refs/test/probe-race',os.environ['GIT_PROBE_VAULT_HEAD']],check=True); assert subprocess.check_output([git,'-C',os.environ['GIT_PROBE_VAULT'],'for-each-ref','--format=%(refname)','refs/test/probe-race'],text=True).strip() == 'refs/test/probe-race'\n"
        " elif hook=='source_fetch_head': open(os.environ['GIT_PROBE_SOURCE_FETCH_HEAD'],'w').write('1'*40)\n"
        " elif hook=='vault_fetch_head': open(os.environ['GIT_PROBE_VAULT_FETCH_HEAD'],'w').write('2'*40)\n"
        " elif hook=='source_detached': subprocess.run([git,'-C',os.environ['GIT_PROBE_SOURCE'],'checkout','--detach',os.environ['GIT_PROBE_SOURCE_HEAD']],check=True,stdout=subprocess.DEVNULL)\n"
        " elif hook=='vault_detached': subprocess.run([git,'-C',os.environ['GIT_PROBE_VAULT'],'checkout','--detach',os.environ['GIT_PROBE_VAULT_HEAD']],check=True,stdout=subprocess.DEVNULL)\n"
        " elif hook=='remote_lock': subprocess.run([git,'-C',os.environ['GIT_PROBE_SEED'],'tag','grim-dawn-sync-active'],check=True); subprocess.run([git,'-C',os.environ['GIT_PROBE_SEED'],'push','origin','refs/tags/grim-dawn-sync-active'],check=True)\n"
        " elif hook=='remote_main': subprocess.run([git,'-C',os.environ['GIT_PROBE_SEED'],'commit','--allow-empty','-m','race'],check=True); subprocess.run([git,'-C',os.environ['GIT_PROBE_SEED'],'push','origin','main'],check=True)\n", encoding="ascii")
    source_package = source / "src" / "grim_dawn_sync"
    source_package.mkdir(parents=True)
    shutil.copy2(package / "__init__.py", source_package / "__init__.py")
    shutil.copy2(package / "__main__.py", source_package / "__main__.py")
    _git(source, "add", "src/grim_dawn_sync")
    _git(source, "commit", "-m", "source package")
    pth = tool_python.parents[1] / "Lib" / "site-packages" / "grim_dawn_sync_source.pth"
    pth.parent.mkdir(parents=True)
    pth.write_bytes(str(source / "src").encode("utf-8") + os.linesep.encode("ascii"))
    wrapper = base / "bin"; wrapper.mkdir()
    (wrapper / "git.cmd").write_text(
        "@echo off\r\n"
        "if \"%~3\"==\"ls-remote\" if \"%GIT_PROBE_HOOK%\"==\"remote_empty\" exit /b 0\r\n"
        "if \"%~3\"==\"ls-remote\" if \"%GIT_PROBE_HOOK%\"==\"remote_duplicate_main\" (echo %GIT_PROBE_BASELINE%\trefs/heads/main&echo %GIT_PROBE_BASELINE%\trefs/heads/main&exit /b 0)\r\n"
        "if \"%~3\"==\"ls-remote\" if \"%GIT_PROBE_HOOK%\"==\"remote_lock\" (echo %GIT_PROBE_BASELINE%\trefs/heads/main&echo %GIT_PROBE_BASELINE%\trefs/tags/grim-dawn-sync-active&exit /b 0)\r\n"
        "if \"%~3\"==\"ls-remote\" if \"%GIT_PROBE_HOOK%\"==\"remote_malformed\" (echo malformed&exit /b 0)\r\n"
        "if not \"%~3\"==\"ls-remote\" goto show\r\n"
        f'"{REAL_GIT}" %*\r\n'
        "if \"%GIT_PROBE_HOOK%\"==\"source_ref\" call \"%GIT_PROBE_GIT%\" -C \"%GIT_PROBE_SOURCE%\" update-ref refs/heads/probe %GIT_PROBE_BASELINE%\r\n"
        "if \"%GIT_PROBE_HOOK%\"==\"vault_ref\" call \"%GIT_PROBE_GIT%\" -C \"%GIT_PROBE_VAULT%\" update-ref refs/heads/probe %GIT_PROBE_BASELINE%\r\n"
        "if \"%GIT_PROBE_HOOK%\"==\"source_fetch_head\" echo 1111111111111111111111111111111111111111>\"%GIT_PROBE_SOURCE_FETCH_HEAD%\"\r\n"
        "if \"%GIT_PROBE_HOOK%\"==\"vault_fetch_head\" echo 2222222222222222222222222222222222222222>\"%GIT_PROBE_VAULT_FETCH_HEAD%\"\r\n"
        "exit /b 0\r\n"
        ":show\r\n"
        "if not \"%~3\"==\"show\" goto real\r\n"
        "if \"%GIT_SUMMARY_HOOK%\"==\"\" goto real\r\n"
        f'"{REAL_GIT}" %*\r\n'
        "if \"%GIT_SUMMARY_HOOK%\"==\"config\" echo x>>\"%GIT_SUMMARY_CONFIG%\"\r\n"
        "if \"%GIT_SUMMARY_HOOK%\"==\"state\" echo x>>\"%GIT_SUMMARY_STATE%\"\r\n"
        "if \"%GIT_SUMMARY_HOOK%\"==\"source_ref\" call \"%GIT_SUMMARY_GIT%\" -C \"%GIT_SUMMARY_SOURCE%\" commit --allow-empty -m hook 1>nul 2>nul\r\n"
        "if \"%GIT_SUMMARY_HOOK%\"==\"vault_ref\" call \"%GIT_SUMMARY_GIT%\" -C \"%GIT_SUMMARY_VAULT%\" tag hook\r\n"
        "if \"%GIT_SUMMARY_HOOK%\"==\"remote_main\" call \"%GIT_SUMMARY_GIT%\" -C \"%GIT_SUMMARY_SEED%\" commit --allow-empty -m hook 1>nul 2>nul\r\n"
        "if \"%GIT_SUMMARY_HOOK%\"==\"remote_main\" call \"%GIT_SUMMARY_GIT%\" -C \"%GIT_SUMMARY_SEED%\" push origin main 1>nul 2>nul\r\n"
        "exit /b 0\r\n:real\r\n" + f'"{REAL_GIT}" %*\r\nexit /b %errorlevel%\r\n', encoding="ascii", newline="")
    # Use the real Git executable.  Race injection is performed by the fixed
    # local mock CLI after its second status/doctor reply, so the production
    # Git invocation path is never intercepted.
    counter = base / "probe-count"
    env=os.environ.copy(); env.update(USERPROFILE=str(profile), LOCALAPPDATA=str(local), PYTHONPATH=str(stub)+os.pathsep+str(ROOT / "src"), PYTHONHOME=str(interp), PATH=str(wrapper)+os.pathsep+env["PATH"], GIT_SHIM_DIR=str(wrapper), REAL_GIT=str(REAL_GIT), STAGE_PROBE_FAULT="", GIT_SUMMARY_HOOK="", GIT_SUMMARY_CONFIG=str(config), GIT_SUMMARY_STATE=str(state), GIT_SUMMARY_GIT=str(REAL_GIT), GIT_SUMMARY_SOURCE=str(source), GIT_SUMMARY_VAULT=str(vault), GIT_SUMMARY_SEED=str(seed), GIT_PROBE_HOOK="", GIT_PROBE_SOURCE=str(source), GIT_PROBE_VAULT=str(vault), GIT_PROBE_SOURCE_HEAD=_git(source, "rev-parse", "HEAD"), GIT_PROBE_VAULT_HEAD=_git(vault, "rev-parse", "HEAD"), GIT_PROBE_SEED=str(seed), GIT_PROBE_CONFIG=str(config), GIT_PROBE_STATE=str(state), GIT_PROBE_LIVE=str(live), GIT_PROBE_PACKAGE_MAIN=str(pth), GIT_PROBE_COUNTER=str(counter), GIT_PROBE_SOURCE_FETCH_HEAD=str(source / ".git" / "FETCH_HEAD"), GIT_PROBE_VAULT_FETCH_HEAD=str(vault / ".git" / "FETCH_HEAD"))
    for command in ("status", "doctor"):
        checked = subprocess.run([str(tool_python), "-m", "grim_dawn_sync", "--config", str(config), "--json", command], env=env, capture_output=True, text=True, encoding="utf-8")
        assert checked.returncode == 0, checked.stderr
        payload = json.loads(checked.stdout)
        if command == "status":
            assert {key: payload[key] for key in ("schema_version","command","readiness","vault_relation","active_lock","recovery_phase","processes","last_pushed_commit")} == {"schema_version":"1.0.0","command":"status","readiness":"blocked", "vault_relation":"remote_changed_or_unknown", "active_lock":None, "recovery_phase":None, "processes":{"complete":True,"status":"clear"}, "last_pushed_commit":baseline}
        else: assert payload["schema_version"] == "1.0.0" and payload["command"] == "doctor" and payload["read_only"] is True and payload["machine_id"] == "desktop-a" and payload["passed"] is True and payload["checks"]["save_root"]["manifest"]["root_hash"] == live_root
    counter.write_text("0", encoding="ascii")
    env["GIT_PROBE_DOCTOR_ROOT"] = live_root
    return env, {"vault":str(vault),"config":str(config),"state":str(state),"source":str(source),"remote":str(remote),"baseline":baseline,"remote_head":remote_head,"remote_root":remote_root,"live_root":live_root}


def test_remote_diff_summary_runs_ps51_with_real_validated_snapshots_and_exact_categories() -> None:
    files={"main/Hero/player.gdc":b"x"*4096, "main/Hero/quests.gdd":b"y"*65536, "transfer.gst":b"z"*1048577, "new.bin":b""}
    with tempfile.TemporaryDirectory(prefix="terminal-a-summary-") as raw:
        env, paths=_summary_case(Path(raw), remote_files=files); result=_invoke(_summary_script(Path(raw)),env)
        assert result.returncode == 0 and result.stderr == ""
        row=json.loads(result.stdout); assert row["sentinel"] == "TERMINAL_A_REMOTE_DIFF_SUMMARY" and row["code"] == "remote_diff_summarized"
        assert row["live_vs_remote"] == row["baseline_vs_remote"] == "different"
        assert row["character_core"] == {"any_change":True,"added":0,"removed":0,"changed":1,"changed_size_bucket":"le_4k"}
        assert row["character_tree_other"] == {"any_change":True,"added":0,"removed":0,"changed":1,"changed_size_bucket":"le_64k"}
        assert row["outside_character_tree"] == {"any_change":True,"added":1,"removed":0,"changed":1,"changed_size_bucket":"gt_1m"}
        for secret in (str(Path(raw)), paths["remote"], paths["baseline"], paths["remote_head"], "transfer.gst", "stderr"):
            assert secret not in result.stdout


@pytest.mark.parametrize("corrupt", ["root", "blob", "tree"])
def test_remote_diff_summary_rejects_invalid_validated_snapshot(corrupt: str) -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-summary-invalid-") as raw:
        env, _ = _summary_case(Path(raw), remote_files={"main/Hero/player.gdc":b"next"}, corrupt=corrupt); result=_invoke(_summary_script(Path(raw)),env)
        assert result.returncode == 1 and result.stderr == ""; assert json.loads(result.stdout)["code"] == "observation_changed"


@pytest.mark.parametrize("hook", ["config", "state", "source_ref", "vault_ref", "remote_main"])
def test_remote_diff_summary_fails_closed_when_observation_changes(hook: str) -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-summary-race-") as raw:
        env, _ = _summary_case(Path(raw), remote_files={"main/Hero/player.gdc":b"next"}); env["GIT_SUMMARY_HOOK"] = hook; result=_invoke(_summary_script(Path(raw)),env)
        assert result.returncode == 1 and result.stderr == ""; assert json.loads(result.stdout)["code"] == "observation_changed"


def test_post_selector_failure_probe_runs_ps51_against_local_bare_remote_and_mock_cli() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-postfailure-") as raw:
        base = Path(raw)
        env, paths = _summary_case(base, remote_files={"main/Hero/player.gdc": b"next"})
        tool = base / "localappdata" / "GrimDawnSaveSyncTool" / ".venv"
        package = tool / "Lib" / "site-packages" / "grim_dawn_sync"
        package.mkdir(parents=True)
        shutil.copy2(base / "stub" / "grim_dawn_sync" / "__init__.py", package / "__init__.py")
        shutil.copy2(base / "stub" / "grim_dawn_sync" / "__main__.py", package / "__main__.py")
        env["PYTHONPATH"] = str(package.parent) + os.pathsep + str(ROOT / "src")
        shortcut = base / "profile" / "Desktop" / "Grim Dawn (DPYes + Save Selection).lnk"
        shortcut.parent.mkdir(parents=True, exist_ok=True); shortcut.write_bytes(b"fixture")
        before = {name: Path(value).read_bytes() for name, value in paths.items() if name in {"config", "state"}}
        result = _invoke(_post_failure_probe_script(base), env)
        assert result.returncode == 0 and result.stderr == ""
        row = json.loads(result.stdout)
        assert row["sentinel"] == "TERMINAL_A_POST_SELECTOR_FAILURE_PROBE"
        assert row["status"] == "complete" and row["code"] == "post_failure_probe_complete"
        assert row["safe_to_retry"] is True and row["live_unchanged"] is True
        assert row["selector_window_count_bucket"] == "zero"
        assert {name: Path(paths[name]).read_bytes() for name in before} == before
        for secret in (str(base), paths["remote"], paths["baseline"], "stderr"):
            assert secret not in result.stdout


@pytest.mark.parametrize("hook", ["source_ref", "vault_ref", "source_fetch_head", "vault_fetch_head", "source_detached", "vault_detached"])
def test_post_selector_failure_probe_rejects_source_or_vault_observation_drift(hook: str) -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-postfailure-drift-") as raw:
        base = Path(raw)
        env, paths = _summary_case(base, remote_files={"main/Hero/player.gdc": b"next"})
        tool = base / "localappdata" / "GrimDawnSaveSyncTool" / ".venv"
        package = tool / "Lib" / "site-packages" / "grim_dawn_sync"
        package.mkdir(parents=True)
        shutil.copy2(base / "stub" / "grim_dawn_sync" / "__init__.py", package / "__init__.py")
        shutil.copy2(base / "stub" / "grim_dawn_sync" / "__main__.py", package / "__main__.py")
        env["PYTHONPATH"] = str(package.parent) + os.pathsep + str(ROOT / "src")
        shortcut = base / "profile" / "Desktop" / "Grim Dawn (DPYes + Save Selection).lnk"
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        shortcut.write_bytes(b"fixture")
        env["GIT_PROBE_HOOK"] = hook
        env["PATH"] = os.environ["PATH"]
        assert json.loads(Path(paths["config"]).read_text(encoding="utf-8"))["vault_repo"] == env["GIT_PROBE_VAULT"]
        result = _invoke(_post_failure_probe_script(base), env)

        row = json.loads(result.stdout)
        assert Path(env["GIT_PROBE_COUNTER"]).read_text() == "5"
        if hook == "vault_ref":
            assert "refs/test/probe-race" in _git(Path(paths["vault"]), "for-each-ref", "--format=%(refname)", "refs")
        assert result.returncode == 1 and result.stderr == ""
        assert row["code"] == "observation_changed"
        assert row["safe_to_retry"] is False
        assert "live_unchanged" not in row
        for secret in (str(base), paths["remote"], paths["baseline"], "FETCH_HEAD"):
            assert secret not in result.stdout


@pytest.mark.parametrize("hook", ["config", "state", "live", "remote_lock"])
def test_post_selector_failure_probe_rejects_real_local_or_remote_mutation(hook: str) -> None:
    """The fixed local CLI mutates only after its second stable reply."""
    with tempfile.TemporaryDirectory(prefix="terminal-a-postfailure-real-race-") as raw:
        base = Path(raw)
        env, _paths = _summary_case(base, remote_files={"main/Hero/player.gdc": b"next"})
        tool = base / "localappdata" / "GrimDawnSaveSyncTool" / ".venv"
        package = tool / "Lib" / "site-packages" / "grim_dawn_sync"
        package.mkdir(parents=True)
        shutil.copy2(base / "stub" / "grim_dawn_sync" / "__init__.py", package / "__init__.py")
        shutil.copy2(base / "stub" / "grim_dawn_sync" / "__main__.py", package / "__main__.py")
        if hook == "live":
            package.joinpath("manifest.py").write_text(
                "import os\nfrom pathlib import Path\n"
                "def build_manifest(*args, **kwargs):\n"
                " if os.environ.get('GIT_PROBE_HOOK') == 'live' and Path(os.environ['GIT_PROBE_COUNTER']).read_text() == '4':\n"
                "  Path(os.environ['GIT_PROBE_LIVE'], 'race.bin').write_bytes(b'x')\n"
                " return {'root_hash': os.environ['GIT_PROBE_DOCTOR_ROOT']}\n",
                encoding="ascii",
            )
        env["PYTHONPATH"] = str(package.parent) + os.pathsep + str(ROOT / "src")
        shortcut = base / "profile" / "Desktop" / "Grim Dawn (DPYes + Save Selection).lnk"
        shortcut.parent.mkdir(parents=True, exist_ok=True); shortcut.write_bytes(b"fixture")
        env["GIT_PROBE_HOOK"] = hook
        env["PATH"] = os.environ["PATH"]
        result = _invoke(_post_failure_probe_script(base), env)
        row = json.loads(result.stdout)
        assert result.returncode == 1 and result.stderr == ""
        assert row["code"] == "observation_changed"
        assert row["safe_to_retry"] is False and "live_unchanged" not in row


@pytest.mark.parametrize("hook", ["remote_lock"])
def test_post_selector_failure_probe_rejects_invalid_remote_advertisement(hook: str) -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-postfailure-remote-") as raw:
        base = Path(raw)
        env, _paths = _summary_case(base, remote_files={"main/Hero/player.gdc": b"next"})
        tool = base / "localappdata" / "GrimDawnSaveSyncTool" / ".venv"
        package = tool / "Lib" / "site-packages" / "grim_dawn_sync"
        package.mkdir(parents=True)
        shutil.copy2(base / "stub" / "grim_dawn_sync" / "__init__.py", package / "__init__.py")
        shutil.copy2(base / "stub" / "grim_dawn_sync" / "__main__.py", package / "__main__.py")
        env["PYTHONPATH"] = str(package.parent) + os.pathsep + str(ROOT / "src")
        shortcut = base / "profile" / "Desktop" / "Grim Dawn (DPYes + Save Selection).lnk"
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        shortcut.write_bytes(b"fixture")
        env["GIT_PROBE_HOOK"] = hook

        result = _invoke(_post_failure_probe_script(base), env)

        row = json.loads(result.stdout)
        assert result.returncode == 1 and result.stderr == ""
        assert row["code"] in {"precondition_failed", "observation_changed"}
        assert row["safe_to_retry"] is False


@pytest.mark.parametrize("fault", ["doctor_root", "installed_manifest_root"])
def test_post_selector_failure_probe_requires_doctor_and_installed_manifest_root_to_match(fault: str) -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-postfailure-root-") as raw:
        base = Path(raw)
        env, paths = _summary_case(base, remote_files={"main/Hero/player.gdc": b"next"})
        tool = base / "localappdata" / "GrimDawnSaveSyncTool" / ".venv"
        package = tool / "Lib" / "site-packages" / "grim_dawn_sync"
        package.mkdir(parents=True)
        shutil.copy2(base / "stub" / "grim_dawn_sync" / "__init__.py", package / "__init__.py")
        main = base / "stub" / "grim_dawn_sync" / "__main__.py"
        if fault == "doctor_root":
            main.write_text(main.read_text(encoding="ascii").replace(f"root={paths['live_root']!r}", "root='0' * 64"), encoding="ascii")
            shutil.copy2(main, package / "__main__.py")
        else:
            shutil.copy2(main, package / "__main__.py")
            (package / "manifest.py").write_text(
                "def build_manifest(*args, **kwargs): return {'root_hash': 'f' * 64}\n", encoding="ascii"
            )
        env["PYTHONPATH"] = str(package.parent) + os.pathsep + str(ROOT / "src")
        shortcut = base / "profile" / "Desktop" / "Grim Dawn (DPYes + Save Selection).lnk"
        shortcut.parent.mkdir(parents=True, exist_ok=True)
        shortcut.write_bytes(b"fixture")

        result = _invoke(_post_failure_probe_script(base), env)

        row = json.loads(result.stdout)
        assert result.returncode == 1 and result.stderr == ""
        assert row["code"] == "precondition_failed"
        assert row["safe_to_retry"] is False


def test_remote_diff_summary_block_parses_in_windows_powershell_51() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-summary-parse-") as raw:
        script = _summary_script(Path(raw)); parser = Path(raw) / "parse.ps1"
        parser.write_text("$tokens=$null;$errors=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$tokens,[ref]$errors);if($errors.Count){exit 1}", encoding="utf-8-sig")
        result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(parser), str(script)], capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("section_marker", "function_marker", "next_function", "call"),
    [
        (
            "## Operator-mediated public remote request",
            "function Invoke-GitLines",
            "function Get-OneGitLine",
            "$x=@(Invoke-GitLines @('status'))",
        ),
        (
            "## Copy-paste execution",
            "function Invoke-GitLines",
            "function Get-OneGitLine",
            "$x=@(Invoke-GitLines -Repo $repo -CommandArgs @('status'))",
        ),
        (
            "## Copy-paste execution: validated remote diff summary",
            "function GitLines",
            "function GitOne",
            "$x=@(GitLines -Repo $repo -CommandArgs @('status'))",
        ),
    ],
)
def test_git_line_helpers_accept_exit_zero_stderr_in_windows_powershell_51(
    section_marker: str, function_marker: str, next_function: str, call: str
) -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-stderr-warning-") as raw:
        base = Path(raw)
        shim = base / "git.cmd"
        shim.write_text(
            "@echo off\r\necho harmless warning 1>&2\r\necho value\r\nexit /b 0\r\n",
            encoding="ascii",
        )
        section = RUNBOOK.read_text(encoding="utf-8").split(section_marker, 1)[1]
        block = section.split("```powershell", 1)[1].split("```", 1)[0]
        helper = block[block.index(function_marker):block.index(next_function)]
        script = base / "stderr-warning.ps1"
        script.write_text(
            "$ErrorActionPreference='Stop'\n$source=$env:WARNING_REPO\n$repo=$env:WARNING_REPO\n"
            + helper
            + "\n"
            + call
            + "\nif($ErrorActionPreference -cne 'Stop' -or $x.Count -ne 1 -or $x[0] -cne 'value'){exit 2}\nWrite-Output ok\n",
            encoding="utf-8-sig",
        )
        env = os.environ.copy()
        env["WARNING_REPO"] = str(base)
        env["PATH"] = str(base) + os.pathsep + env["PATH"]
        result = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == "ok\n" and result.stderr == ""


def test_every_executable_runbook_powershell_block_parses_in_windows_powershell_51() -> None:
    blocks = re.findall(r"```powershell\r?\n(.*?)```", RUNBOOK.read_text(encoding="utf-8"), re.DOTALL)
    assert len(blocks) >= 5
    with tempfile.TemporaryDirectory(prefix="terminal-a-all-parse-") as raw:
        base = Path(raw)
        parser = base / "parse.ps1"
        parser.write_text(
            "$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$t,[ref]$e);if($e.Count){$e|ForEach-Object{$_.Message};exit 1}",
            encoding="utf-8-sig",
        )
        for index, block in enumerate(blocks):
            script = base / f"block-{index}.ps1"
            script.write_text(block, encoding="utf-8-sig")
            result = subprocess.run(
                [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(parser), str(script)],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            assert result.returncode == 0, f"block {index}: {result.stdout}{result.stderr}"


def test_post_failure_probe_remote_advertisement_detects_real_main_race() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-postfailure-main-race-") as raw:
        base = Path(raw)
        env, paths = _summary_case(base, remote_files={"main/Hero/player.gdc": b"next"})
        block = RUNBOOK.read_text(encoding="utf-8").split("## Post-selector-failure readonly probe", 1)[1].split("```powershell", 1)[1].split("```", 1)[0]
        helpers = block[block.index("function Q"):block.index("function Json")]
        script = base / "remote-main-race.ps1"
        script.write_text(
            "$vault=$env:GIT_PROBE_VAULT\n" + helpers + "\n"
            "$r1=RemoteAdvertisement $vault\n"
            "& $env:REAL_GIT -C $env:GIT_PROBE_SEED commit --allow-empty -m race 1>$null 2>$null\n"
            "if($LASTEXITCODE -ne 0){exit 2}\n"
            "& $env:REAL_GIT -C $env:GIT_PROBE_SEED push origin main 1>$null 2>$null\n"
            "if($LASTEXITCODE -ne 0){exit 3}\n"
            "$r2=RemoteAdvertisement $vault\n"
            "[ordered]@{different=($r1 -cne $r2);one=$true;code=$(if($r1 -cne $r2){'observation_changed'}else{'unexpected_failed'})}|ConvertTo-Json -Compress\n",
            encoding="utf-8-sig",
        )
        result = _invoke(script, env)
        assert result.returncode == 0 and result.stderr == "", (result.returncode, result.stdout, result.stderr)
        row = json.loads(result.stdout)
        assert row == {"different": True, "one": True, "code": "observation_changed"}
        assert paths["remote"] not in result.stdout


def test_sequence_8_selector_automation_is_not_an_executable_powershell_block() -> None:
    section = RUNBOOK.read_text(encoding="utf-8").split(
        "## Retired selector cancel/reload automation (sequence 8; DO NOT RUN)", 1
    )[1].split("Continue only when", 1)[0]
    assert "```powershell" not in section
    assert "```text" in section
    assert "operator must perform the visible Esc/F5 actions manually" in section


def test_sequence_9_request_block_parses_in_windows_powershell_51() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-seq9-parse-") as raw:
        script = _post_failure_probe_script(Path(raw)); parser = Path(raw) / "parse.ps1"
        parser.write_text("$tokens=$null;$errors=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$tokens,[ref]$errors);if($errors.Count){exit 1}", encoding="utf-8-sig")
        result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(parser), str(script)], capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stderr


def _prepare_stage_case(base: Path) -> tuple[dict[str, str], dict[str, str]]:
    env, paths = _summary_case(base, remote_files={"main/Hero/player.gdc": b"next"})
    source = Path(paths["source"])
    package = base / "localappdata" / "GrimDawnSaveSyncTool" / ".venv" / "Lib" / "site-packages" / "grim_dawn_sync"
    package.mkdir(parents=True)
    shutil.copy2(base / "stub" / "grim_dawn_sync" / "__init__.py", package / "__init__.py")
    shutil.copy2(base / "stub" / "grim_dawn_sync" / "__main__.py", package / "__main__.py")
    pth = package.parent / "grim_dawn_sync_source.pth"
    pth.write_bytes(str(source / "src").encode("utf-8") + os.linesep.encode("ascii"))
    env["PYTHONPATH"] = str(package.parent) + os.pathsep + str(ROOT / "src")
    env["GIT_PROBE_PACKAGE_MAIN"] = str(pth)
    env["PATH"] = os.environ["PATH"]
    shortcut = base / "profile" / "Desktop" / "Grim Dawn (DPYes + Save Selection).lnk"
    shortcut.parent.mkdir(parents=True, exist_ok=True); shortcut.write_bytes(b"fixture")
    return env, paths


def _assert_stage_failure(result: subprocess.CompletedProcess[str], stage: str, code: str) -> None:
    assert result.returncode == 1 and result.stderr == ""
    assert result.stdout.count("\n") == 1
    row = json.loads(result.stdout)
    assert row == {"sentinel":"TERMINAL_A_POST_SELECTOR_FAILURE_STAGE_PROBE","status":"blocked","leg":"A1","machine_id":"desktop-a","stage":stage,"code":code}


def test_sequence_10_stage_probe_accepts_volatile_fields_with_same_semantic_projection() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-stage-success-") as raw:
        base = Path(raw); env, paths = _prepare_stage_case(base)
        script = _stage_probe_script(base)
        block = script.read_text(encoding="utf-8-sig")
        for forbidden in ("Start-Process", "SendKeys", "CloseMainWindow", " fetch ", " push ", " checkout ", "update-ref"):
            assert forbidden not in block
        result = _invoke(script, env)
        assert result.returncode == 0 and result.stderr == "" and result.stdout.count("\n") == 1
        row = json.loads(result.stdout)
        assert row == {"sentinel":"TERMINAL_A_POST_SELECTOR_FAILURE_STAGE_PROBE","status":"complete","leg":"A1","machine_id":"desktop-a","stage":"complete","code":"stage_probe_complete","relation":"unknown","remote_lock":"clear","processes":"clear","selector_window_count_bucket":"zero"}
        for secret in (str(base), paths["remote"], paths["baseline"], "stderr"): assert secret not in result.stdout


@pytest.mark.parametrize(("fault","stage","code"), [
    ("status_command_failed","status_first","status_command_failed"),
    ("status_shape_invalid","status_first","status_shape_invalid"),
    ("doctor_command_failed","doctor_first","doctor_command_failed"),
    ("doctor_shape_invalid","doctor_first","doctor_shape_invalid"),
    ("installed_manifest_mismatch","live_manifest","installed_manifest_mismatch"),
    ("status_not_stable","status_second","status_not_stable"),
    ("status_schema_drift","status_second","status_not_stable"),
    ("status_command_drift","status_second","status_not_stable"),
    ("process_complete_drift","status_second","status_not_stable"),
    ("doctor_not_stable","doctor_second","doctor_not_stable"),
    ("doctor_read_only_drift","doctor_second","doctor_not_stable"),
    ("post_invariant_changed","post_invariant","post_invariant_changed"),
])
def test_sequence_10_stage_probe_maps_cli_and_invariant_failures(fault: str, stage: str, code: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f"terminal-a-stage-{fault}-") as raw:
        base = Path(raw); env, _paths = _prepare_stage_case(base); env["STAGE_PROBE_FAULT"] = fault
        _assert_stage_failure(_invoke(_stage_probe_script(base), env), stage, code)


def test_sequence_10_stage_probe_maps_remote_lock_and_process_observation() -> None:
    for fault in ("remote_missing", "remote_lock", "process"):
        with tempfile.TemporaryDirectory(prefix=f"terminal-a-stage-{fault}-") as raw:
            base = Path(raw); env, _paths = _prepare_stage_case(base); script = _stage_probe_script(base)
            if fault == "remote_missing":
                _git(Path(_paths["remote"]), "update-ref", "-d", "refs/heads/main")
                expected = ("remote_advertisement", "remote_advertisement_invalid")
            elif fault == "remote_lock":
                _git(Path(env["GIT_PROBE_SEED"]), "tag", "grim-dawn-sync-active")
                _git(Path(env["GIT_PROBE_SEED"]), "push", "origin", "refs/tags/grim-dawn-sync-active")
                expected = ("remote_advertisement", "remote_advertisement_invalid")
            else:
                script.write_text(script.read_text(encoding="utf-8-sig").replace("Get-CimInstance Win32_Process -ErrorAction Stop", "throw 'fixture'"), encoding="utf-8-sig")
                expected = ("process_window", "process_observation_inconclusive")
            _assert_stage_failure(_invoke(script, env), *expected)


def test_sequence_10_stage_probe_reports_semantic_and_final_process_stages() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-stage-semantic-") as raw:
        base = Path(raw); env, _paths = _prepare_stage_case(base); script = _stage_probe_script(base)
        text = script.read_text(encoding="utf-8-sig")
        text = text.replace(
            "$stage='semantic';if($d1.machine_id-cne$machineId)",
            "$stage='semantic';$d1.machine_id='desktop-b';if($d1.machine_id-cne$machineId)",
        )
        script.write_text(text, encoding="utf-8-sig")
        _assert_stage_failure(_invoke(script, env), "semantic", "machine_id_unexpected")

    with tempfile.TemporaryDirectory(prefix="terminal-a-stage-final-process-") as raw:
        base = Path(raw); env, _paths = _prepare_stage_case(base); script = _stage_probe_script(base)
        text = script.read_text(encoding="utf-8-sig")
        marker = "Get-CimInstance Win32_Process -ErrorAction Stop"
        before, found, after = text.rpartition(marker)
        assert found
        script.write_text(before + "throw 'fixture'" + after, encoding="utf-8-sig")
        _assert_stage_failure(
            _invoke(script, env), "final_process_window", "process_observation_inconclusive"
        )


@pytest.mark.parametrize(("hook","code"), [
    ("source_detached","post_invariant_changed"),
    ("vault_detached","post_invariant_changed"),
    ("source_fetch_head","post_invariant_changed"),
    ("vault_fetch_head","post_invariant_changed"),
    ("live","post_invariant_changed"),
    ("package_py","post_invariant_changed"),
    ("remote_main","remote_advertisement_invalid"),
    ("remote_lock","remote_advertisement_invalid"),
])
def test_sequence_10_stage_probe_detects_late_mutations(hook: str, code: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f"terminal-a-stage-late-{hook}-") as raw:
        base=Path(raw); env,_paths=_prepare_stage_case(base); script=_stage_probe_script(base)
        if hook in {"remote_main","remote_lock"}:
            env["GIT_PROBE_REMOTE"]=_paths["remote"]
            if hook=="remote_main":
                env["GIT_PROBE_BASELINE"]=_paths["baseline"]
                command="& $env:REAL_GIT -C $env:GIT_PROBE_REMOTE update-ref refs/heads/main $env:GIT_PROBE_BASELINE 1>$null 2>$null"
            else:
                command="$oid=@(& $env:REAL_GIT -C $env:GIT_PROBE_REMOTE rev-parse refs/heads/main)[0];& $env:REAL_GIT -C $env:GIT_PROBE_REMOTE update-ref refs/tags/grim-dawn-sync-active $oid 1>$null 2>$null"
            injection = command+";$finalRemote=Remote $vault"
            script.write_text(script.read_text(encoding="utf-8-sig").replace("$finalRemote=Remote $vault",injection),encoding="utf-8-sig")
        else:
            env["GIT_PROBE_HOOK"]=hook
        _assert_stage_failure(_invoke(script,env),"post_invariant",code)


def test_sequence_10_stage_probe_detects_late_doctor_root_and_final_selector_count() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-stage-late-doctor-") as raw:
        base=Path(raw); env,_paths=_prepare_stage_case(base); env["STAGE_PROBE_FAULT"]="late_doctor_root"
        _assert_stage_failure(_invoke(_stage_probe_script(base),env),"post_invariant","post_invariant_changed")
    with tempfile.TemporaryDirectory(prefix="terminal-a-stage-window-") as raw:
        base=Path(raw); env,_paths=_prepare_stage_case(base); script=_stage_probe_script(base)
        script.write_text(script.read_text(encoding="utf-8-sig").replace("[StageProbeWindow]::Count($selectorTitle)","1"),encoding="utf-8-sig")
        result=_invoke(script,env); row=json.loads(result.stdout)
        assert result.returncode==0 and result.stderr=="" and row["selector_window_count_bucket"]=="one"


def test_sequence_10_native_timeout_kills_mock_child_and_descendant() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-stage-timeout-") as raw:
        base=Path(raw); pidfile=base/"pids"; hang=base/"hang.py"
        hang.write_text("import os,subprocess,sys,time\np=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,stdin=subprocess.DEVNULL)\nopen(sys.argv[1],'w').write(str(os.getpid())+' '+str(p.pid))\ntime.sleep(60)\n",encoding="ascii")
        block=RUNBOOK.read_text(encoding="utf-8").split("## Post-selector-failure stage probe (sequence 10)",1)[1].split("```powershell",1)[1].split("```",1)[0]
        helpers=block[block.index("function Q"):block.index("function Git")].replace("WaitForExit(60000)","WaitForExit(250)")
        script=base/"timeout.ps1"; script.write_text(helpers+"\ntry{Native $env:TIMEOUT_PYTHON @($env:TIMEOUT_HANG,$env:TIMEOUT_PIDS) 'timeout_blocked'}catch{Write-Output $_.Exception.Message;exit 1}\n",encoding="utf-8-sig")
        env=os.environ.copy(); env.update(TIMEOUT_PYTHON=sys.executable,TIMEOUT_HANG=str(hang),TIMEOUT_PIDS=str(pidfile))
        result=_invoke(script,env)
        assert result.returncode==1 and result.stderr=="" and result.stdout=="timeout_blocked\n"
        pids=[int(value) for value in pidfile.read_text(encoding="ascii").split()]; assert len(pids)==2
        for pid in pids:
            check=subprocess.run([str(POWERSHELL),"-NoProfile","-NonInteractive","-Command",f"if(Get-Process -Id {pid} -ErrorAction SilentlyContinue){{exit 1}}"],capture_output=True,text=True,encoding="utf-8")
            assert check.returncode==0


def test_sequence_10_stage_probe_parses_in_windows_powershell_51() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-stage-parse-") as raw:
        base=Path(raw); script=_stage_probe_script(base); parser=base/"parse.ps1"
        parser.write_text("$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$t,[ref]$e);if($e.Count){$e|ForEach-Object{$_.Message};exit 1}",encoding="utf-8-sig")
        result=subprocess.run([str(POWERSHELL),"-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-File",str(parser),str(script)],capture_output=True,text=True,encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr


def _prepare_source_path_repair_case(
    base: Path, *, existing_pth: bytes | None = None, import_fails: bool = False
) -> tuple[dict[str, str], Path]:
    profile = base / "profile"
    source_package = profile / "grimdawnrep" / "src" / "grim_dawn_sync"
    source_package.mkdir(parents=True)
    (source_package / "__init__.py").write_text("\n", encoding="ascii")
    main = "raise RuntimeError('fixture')\n" if import_fails else "\n"
    (source_package / "__main__.py").write_text(main, encoding="ascii")
    venv = base / "localappdata" / "GrimDawnSaveSyncTool" / ".venv"
    _run(sys.executable, "-m", "venv", "--without-pip", str(venv))
    pth = venv / "Lib" / "site-packages" / "grim_dawn_sync_source.pth"
    if existing_pth is not None:
        pth.write_bytes(existing_pth)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.update(USERPROFILE=str(profile), LOCALAPPDATA=str(base / "localappdata"))
    return env, pth


def _assert_source_path_failure(result: subprocess.CompletedProcess[str], stage: str, code: str) -> None:
    assert result.returncode == 1 and result.stderr == ""
    assert result.stdout.count("\n") == 1
    assert json.loads(result.stdout) == {"sentinel":"TERMINAL_A_SOURCE_PATH_REPAIR","status":"blocked","leg":"A1","machine_id":"desktop-a","stage":stage,"code":code}


def test_sequence_12_source_path_repair_is_atomic_and_idempotent() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-source-path-success-") as raw:
        base = Path(raw); env, pth = _prepare_source_path_repair_case(base, existing_pth=b"wrong\r\n")
        script = _source_path_repair_script(base)
        result = _invoke(script, env)
        assert result.returncode == 0 and result.stderr == "" and result.stdout.count("\n") == 1
        assert json.loads(result.stdout) == {"sentinel":"TERMINAL_A_SOURCE_PATH_REPAIR","status":"complete","leg":"A1","machine_id":"desktop-a","stage":"complete","code":"pth_repaired"}
        expected = str(base / "profile" / "grimdawnrep" / "src").encode("utf-8") + os.linesep.encode("ascii")
        assert pth.read_bytes() == expected
        before = (pth.stat().st_mtime_ns, pth.read_bytes())
        second = _invoke(script, env)
        assert second.returncode == 0 and json.loads(second.stdout)["code"] == "pth_already_current"
        assert (pth.stat().st_mtime_ns, pth.read_bytes()) == before
        for secret in (str(base), str(pth), "grim_dawn_sync_source.pth"):
            assert secret not in result.stdout


@pytest.mark.parametrize("old", [None, b"old bytes must return exactly\x00\xff"])
def test_sequence_12_source_path_repair_rolls_back_exactly_when_import_fails(old: bytes | None) -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-source-path-rollback-") as raw:
        base = Path(raw); env, pth = _prepare_source_path_repair_case(base, existing_pth=old, import_fails=True)
        result = _invoke(_source_path_repair_script(base), env)
        _assert_source_path_failure(result, "import", "source_package_import_failed")
        if old is None:
            assert not pth.exists()
        else:
            assert pth.read_bytes() == old
        assert list(pth.parent.glob(".grim_dawn_sync_source.*")) == []


def test_sequence_13_source_path_repair_preserves_exact_backup_when_rollback_fails() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-source-path-rollback-failure-") as raw:
        base = Path(raw)
        old = b"only exact old pth backup\x00\xff"
        env, pth = _prepare_source_path_repair_case(base, existing_pth=old, import_fails=True)
        script = _source_path_repair_script(base)
        text = script.read_text(encoding="utf-8-sig")
        marker = "if($oldExists){if(!$rollback-or!(Test-Path -LiteralPath $rollback -PathType Leaf)-or!(SameBytes ([IO.File]::ReadAllBytes($rollback)) $oldBytes)){throw 'rollback_failed'};$temp="
        replacement = "if($oldExists){if(!$rollback-or!(Test-Path -LiteralPath $rollback -PathType Leaf)-or!(SameBytes ([IO.File]::ReadAllBytes($rollback)) $oldBytes)){throw 'rollback_failed'};throw 'rollback_failed';$temp="
        assert marker in text
        script.write_text(text.replace(marker, replacement, 1), encoding="utf-8-sig")

        result = _invoke(script, env)

        _assert_source_path_failure(result, "rollback", "rollback_failed")
        backups = list(pth.parent.glob(".grim_dawn_sync_source.*.rollback"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == old


def test_sequence_12_source_path_repair_parses_in_windows_powershell_51() -> None:
    with tempfile.TemporaryDirectory(prefix="terminal-a-source-path-parse-") as raw:
        base=Path(raw); script=_source_path_repair_script(base); parser=base/"parse.ps1"
        parser.write_text("$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$t,[ref]$e);if($e.Count){$e|ForEach-Object{$_.Message};exit 1}",encoding="utf-8-sig")
        result=subprocess.run([str(POWERSHELL),"-NoProfile","-NonInteractive","-ExecutionPolicy","Bypass","-File",str(parser),str(script)],capture_output=True,text=True,encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr
