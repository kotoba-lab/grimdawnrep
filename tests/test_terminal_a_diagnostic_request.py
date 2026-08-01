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
EXPECTED_CHECKS = ["status", "doctor", "vault_remote_classification"]
EXPECTED_CONSTRAINTS = [
    "no_game_launch",
    "no_lock",
    "no_recover",
    "no_restore_snapshot_bookmark_promote",
    "no_commit_push_merge_rebase_reset_checkout",
    "no_state_config_save_remote_ref_write",
    "vault_readonly_status_rev_parse_ls_remote_fetch_merge_base_manifest_compare",
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
    assert payload["sequence"] == 6
    assert payload["target_machine_id"] == "desktop-a"
    assert payload["leg"] == "A1" and payload["observed_code"] == "remote_changed_or_unknown"
    assert payload["action"] == "classify_remote_readonly"
    assert payload["response_sentinel"] == "TERMINAL_A_REMOTE_CLASSIFICATION"
    assert payload["request_id"] == "6414c271-db14-4b57-b579-81bc382c6693"
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


def test_terminal_a_request_has_strict_utc_window_of_at_most_seventy_five_minutes() -> None:
    _raw, payload = _request()
    values: dict[str, datetime] = {}
    for field in ("issued_at", "not_before", "expires_at"):
        value = str(payload[field])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value)
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)
        values[field] = parsed
    assert values["issued_at"] <= values["not_before"] < values["expires_at"]
    assert (values["expires_at"] - values["not_before"]).total_seconds() <= 4500
    assert payload["issued_at"] == payload["not_before"] == "2026-08-01T13:18:00Z"
    assert payload["expires_at"] == "2026-08-01T14:33:00Z"
    assert (values["expires_at"] - values["not_before"]).total_seconds() == 4500


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

    assert "one allow-listed blocked sentinel identifying only the failed stage" in remote_section
    assert "without disclosing the request,\nremote URL, object IDs, local paths, or Git output" in remote_section
    expected_codes = {
        "origin_identity": "origin_identity_invalid",
        "source_branch": "source_branch_invalid",
        "source_clean": "source_clean_invalid",
        "fingerprint": "fingerprint_invalid",
        "fetch": "fetch_failed",
        "oid": "oid_invalid",
        "ancestor": "ancestor_invalid",
        "blob": "blob_invalid",
        "schema": "schema_invalid",
        "time": "time_invalid",
        "post_invariant": "post_invariant_invalid",
    }
    for stage, code in expected_codes.items():
        assert f"{stage} = '{code}'" in command
        assert f"$stage = '{stage}'" in command
    assert "stage = $safeStage" in command
    assert "code = [string]$stageCodes[$safeStage]" in command
    assert "remote_request_invalid" not in command
    assert "Write-Output (Write-RequestBlocked)" in command
    assert "Write-Output $requestRaw" not in command


def test_remote_handoff_sets_each_observable_stage_before_its_check() -> None:
    _remote_section, command = _remote_section_and_block()
    try_block = command.split("try {", 2)[2]
    ordered_stage_checks = (
        ("$stage = 'origin_identity'", "$fetchUrls ="),
        ("$stage = 'source_branch'", "$beforeBranch ="),
        ("$stage = 'source_clean'", "$beforeStatus ="),
        ("$stage = 'fingerprint'", "$beforePython ="),
        ("$stage = 'fetch'", "Invoke-GitQuiet @('fetch'"),
        ("$stage = 'oid'", "$originMaster ="),
        ("$stage = 'ancestor'", "Invoke-GitQuiet @('merge-base'"),
        ("$stage = 'blob'", "$requestLines ="),
        ("$stage = 'schema'", "ConvertFrom-Json -ErrorAction Stop"),
        ("$stage = 'time'", "$issued = Get-RawStrictUtc"),
        ("$stage = 'post_invariant'", "$afterBranch ="),
    )
    positions: list[int] = []
    for stage_marker, check_marker in ordered_stage_checks:
        stage_position = try_block.index(stage_marker)
        check_position = try_block.index(check_marker)
        assert stage_position < check_position
        positions.append(stage_position)
    assert positions == sorted(positions)


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
        "($expires - $notBefore).TotalSeconds -gt 4500",
        "$now -lt $notBefore",
        "$now -ge $expires",
    ):
        assert required in command
    assert "-not ($request.sequence -is [int])" in command
    assert "Get-StrictUtc ([string]$request.issued_at)" not in command


def test_remote_classification_block_is_read_only_and_sanitized() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    block = runbook.split("## Copy-paste execution", 1)[1].split("```powershell", 1)[1].split("```", 1)[0]

    assert "TERMINAL_A_REMOTE_CLASSIFICATION" in block
    assert "--json @CommandArgs" in block
    assert "@('status')" in block and "@('doctor')" in block
    assert "@('ls-remote','--refs','origin','refs/heads/main','refs/tags/grim-dawn-sync-active')" in block
    assert "@('fetch','--no-tags','--no-write-fetch-head','origin',$remoteHead)" in block
    assert "function Test-GitAncestor" in block
    assert block.count("merge-base --is-ancestor") == 1
    assert ".sync/manifest.json" in block
    assert "Get-LocalFingerprint" in block
    assert "Get-FetchHeadFingerprint" in block
    assert "Get-FileFingerprint $State" in block
    assert "'for-each-ref','--sort=refname','--format=%(refname) %(objectname)','refs'" in block
    assert "refs/tags/grim-dawn-sync-active" in block
    assert "$classification = if ($remoteHead -ceq $localHead)" in block
    assert "'remote_ahead'" in block and "'remote_behind'" in block and "'diverged'" in block
    assert "$content = if ($liveRoot -ceq $remoteManifest.root_hash)" in block
    assert "only same content is safe to continue" in block
    assert "$afterLiveRoot -cne $liveRoot" in block
    assert "'safe_remote_ahead'" in block
    assert "'remote_content_differs'" in block
    for forbidden in (" launch", " recover", " restore", " snapshot", " bookmark", " promote", " commit", " push", " rebase", " reset", " checkout"):
        assert forbidden not in block.lower()
