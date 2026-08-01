from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid


ROOT = Path(__file__).resolve().parents[1]
REQUEST_PATH = ROOT / "ops" / "handoff" / "terminal-a-diagnostic-request.v1.json"
RUNBOOK_PATH = ROOT / "docs" / "operations" / "terminal-a-roundtrip-diagnose.md"

EXPECTED_KEYS = {
    "schema_version",
    "kind",
    "request_id",
    "sequence",
    "target_machine_id",
    "leg",
    "observed_code",
    "action",
    "checks",
    "constraints",
    "response_sentinel",
    "issued_at",
    "not_before",
    "expires_at",
}
EXPECTED_CHECKS = ["status", "doctor", "latest_launch_failure"]
EXPECTED_CONSTRAINTS = [
    "no_launch_retry",
    "no_recover",
    "no_save_mutation",
    "no_push",
    "no_pull",
    "no_checkout",
    "no_merge",
    "no_reset",
    "source_fetch_only",
    "source_git_show_only",
    "no_fetched_code_execution",
]
FORBIDDEN_FIELDS = {
    "command",
    "args",
    "script",
    "path",
    "url",
    "ref",
    "hash",
    "save",
    "session",
    "note",
}


def _request() -> tuple[bytes, dict[str, object]]:
    raw = REQUEST_PATH.read_bytes()
    return raw, json.loads(raw.decode("utf-8"))


def _remote_section_and_block() -> tuple[str, str]:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    section = runbook.split("## Operator-mediated public remote request", 1)[1].split("## Boundaries", 1)[0]
    return section, section.split("```powershell", 1)[1].split("```", 1)[0]


def test_terminal_a_request_is_fixed_canonical_small_utf8_lf_json() -> None:
    raw, payload = _request()

    assert REQUEST_PATH.relative_to(ROOT).as_posix() == "ops/handoff/terminal-a-diagnostic-request.v1.json"
    assert len(raw) <= 4096
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw and raw.endswith(b"\n")
    canonical = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert raw.decode("utf-8") == canonical


def test_terminal_a_request_has_exact_schema_identity_and_readonly_action() -> None:
    _raw, payload = _request()

    assert set(payload) == EXPECTED_KEYS
    assert payload["schema_version"] == "1.0.0"
    assert payload["kind"] == "grim_dawn_terminal_diagnostic_request"
    assert payload["sequence"] == 3
    assert payload["target_machine_id"] == "desktop-a"
    assert payload["leg"] == "A1" and payload["observed_code"] == "launch_failed"
    assert payload["action"] == "diagnose_readonly"
    assert payload["response_sentinel"] == "TERMINAL_A_DIAGNOSIS"
    parsed_id = uuid.UUID(str(payload["request_id"]))
    assert str(parsed_id) == payload["request_id"]


def test_terminal_a_request_uses_exact_checks_constraints_and_no_forbidden_fields() -> None:
    raw, payload = _request()

    assert payload["checks"] == EXPECTED_CHECKS
    assert payload["constraints"] == EXPECTED_CONSTRAINTS
    assert not (set(payload) & FORBIDDEN_FIELDS)
    assert all(isinstance(item, str) for item in payload["checks"])
    assert all(isinstance(item, str) for item in payload["constraints"])
    assert b"private" not in raw.lower()


def test_terminal_a_request_has_strict_utc_window_of_at_most_one_hour() -> None:
    _raw, payload = _request()
    values: dict[str, datetime] = {}
    for field in ("issued_at", "not_before", "expires_at"):
        value = str(payload[field])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value)
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)
        values[field] = parsed
    assert values["issued_at"] <= values["not_before"] < values["expires_at"]
    assert (values["expires_at"] - values["not_before"]).total_seconds() <= 3600


def test_remote_handoff_fetches_canonical_master_and_reads_only_fixed_commit_path() -> None:
    remote_section, command = _remote_section_and_block()

    assert "canonical\npublic `origin/master`" in remote_section
    assert "$publicUrl = 'https://github.com/kotoba-lab/grimdawnrep.git'" in command
    assert "@('remote','get-url','--all','origin')" in command
    assert "@('remote','get-url','--push','--all','origin')" in command
    assert "@('fetch','--no-tags','origin','master:refs/remotes/origin/master')" in command
    assert "'fetch','--no-tags','origin','+master:" not in command
    assert "@('rev-parse','origin/master')" in command
    assert "@('rev-parse','FETCH_HEAD')" in command
    assert "@('merge-base','--is-ancestor',$beforeHead,$fetchHead)" in command
    assert "$requestCommit = $fetchHead" in command
    assert 'git -C $source show "$requestCommit`:$requestObjectPath"' in command
    assert "$gitExitCode = $LASTEXITCODE" in command
    assert "if ($gitExitCode -ne 0)" in command


def test_remote_handoff_forbids_source_mutation_execution_and_preserves_a1_boundary() -> None:
    remote_section, source_block = _remote_section_and_block()

    assert "@('fetch','--no-tags','origin','master:refs/remotes/origin/master')" in source_block
    assert ' show "$requestCommit`:$requestObjectPath"' in source_block
    for forbidden in ("'pull'", "'checkout'", "'reset'", "'rebase'", "'push'", " -m grim_dawn_sync"):
        assert forbidden not in source_block.lower()
    assert "Do not execute or import\nfetched code" in remote_section
    assert "no pull, checkout, reset,\nmerge, rebase, push, launch retry, recovery, or save mutation" in remote_section
    assert "$afterHead -cne $beforeHead" in source_block
    assert "$afterBranch -cne $beforeBranch" in source_block
    assert "$afterPython -cne $beforePython" in source_block
    assert "$afterConfig -cne $beforeConfig" in source_block


def test_remote_handoff_relays_only_sanitized_sentinel_without_private_data() -> None:
    remote_section, command = _remote_section_and_block()

    assert "one fixed blocked sentinel" in remote_section
    assert "without disclosing the request,\nremote URL, object IDs, local paths, or Git output" in remote_section
    assert "code = 'remote_request_invalid'" in command
    assert "Write-Output (Write-RequestBlocked)" in command
    assert "Write-Output $requestRaw" not in command


def test_remote_handoff_validates_canonical_json_shape_and_live_window_before_use() -> None:
    _section, command = _remote_section_and_block()

    for required in (
        "$requestBytes -gt 4096",
        "$requestRaw[0] -eq [char]0xFEFF",
        "$requestRaw.Contains([char]0xFFFD)",
        "ConvertFrom-Json -ErrorAction Stop",
        "Assert-ExactArray $actualKeys $expectedKeys",
        "Assert-ExactArray $request.checks",
        "Assert-ExactArray $request.constraints",
        "[Guid]::TryParse",
        "Get-StrictUtc",
        "Get-RawStrictUtc",
        "[Regex]::Matches($Raw",
        "$issued -gt $notBefore",
        "($expires - $notBefore).TotalSeconds -gt 3600",
        "$now -lt $notBefore",
        "$now -ge $expires",
    ):
        assert required in command
    assert "-not ($request.sequence -is [int])" in command
    assert "Get-StrictUtc ([string]$request.issued_at)" not in command
