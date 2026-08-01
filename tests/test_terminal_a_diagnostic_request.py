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
EXPECTED_CHECKS = [
    "status_baseline", "doctor", "current_deployment_contract",
    "remote_ahead_live_equals_baseline", "stable_remote_difference",
    "selector_cancel_reload_invariants",
]
EXPECTED_CONSTRAINTS = [
    "shortcut_ui_cancel_reload_only",
    "no_game_or_dpyes_start",
    "no_lock",
    "no_recover",
    "no_restore_snapshot_bookmark_promote",
    "no_commit_push_merge_rebase_reset_checkout",
    "no_state_config_save_remote_ref_write",
    "allow_only_designated_catalog_tracking_ref_transition",
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
    assert payload["sequence"] == 8
    assert payload["target_machine_id"] == "desktop-a"
    assert payload["leg"] == "A1" and payload["observed_code"] == "remote_changed_or_unknown"
    assert payload["action"] == "selector_cancel_reload_dry_run"
    assert payload["response_sentinel"] == "TERMINAL_A_SELECTOR_DRY_RUN"
    assert payload["request_id"] == "2fb462bc-8b6d-4f09-ae13-0996640dcf07"
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
    assert payload["issued_at"] == payload["not_before"] == "2026-08-02T13:45:00Z"
    assert payload["expires_at"] == "2026-08-02T15:00:00Z"
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


def test_remote_diff_summary_block_uses_only_trusted_local_validation_and_aggregate_output() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    block = runbook.split("## Copy-paste execution: validated remote diff summary", 1)[1].split("```powershell", 1)[1].split("```", 1)[0]

    assert "TERMINAL_A_REMOTE_DIFF_SUMMARY" in block
    assert "validate_commit_snapshot(sys.argv[2])" in block
    assert "$python -c $probe $Vault $Commit" in block
    assert "--no-tags','--no-write-fetch-head','origin',$remote" in block
    assert "ls-remote','--refs','origin" in block
    assert "character_core" in block and "character_tree_other" in block and "outside_character_tree" in block
    assert "changed_size_bucket" in block
    assert "remote_diff_summarized" in block and "observation_changed" in block
    assert "$refs1" in block and "$refs2" in block and "$before -cne $after" in block
    assert "function CompareManifests" in block
    assert "$summary=CompareManifests $baseManifest $remoteManifest" in block
    assert "function Compare($Left,$Right)" not in block
    assert "function GitLines($Repo,[string[]]$CommandArgs)" in block
    assert "GitLines -Repo $vault -CommandArgs @('ls-remote','--refs','origin')" in block
    assert "GitQuiet -Repo $vault -CommandArgs @('fetch','--no-tags','--no-write-fetch-head','origin',$remote)" in block
    assert "System.Collections.Generic.HashSet[string]" in block
    assert "no path, character/account name, object" in runbook


def test_remote_diff_summary_block_keeps_sequence6_classification_block_available() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    classification = runbook.split("## Copy-paste execution", 1)[1].split("## Retired failure-diagnosis reference", 1)[0]
    assert "TERMINAL_A_REMOTE_CLASSIFICATION" in classification
    assert "safe_remote_ahead" in classification


def test_selector_dry_run_allows_only_catalog_ref_to_advertised_remote() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    block = runbook.split("## Selector cancel/reload dry-run block", 1)[1].split("```powershell", 1)[1].split("```", 1)[0]

    assert "TERMINAL_A_SELECTOR_DRY_RUN" in block
    assert "$catalogRef='refs/remotes/origin/main'" in block
    assert "function Stable($Vault,$Remote,$Base,$Before,$After)" in block
    assert "$Before[$catalogRef] -cne $Base" in block
    assert "!$After.Contains($catalogRef)" in block
    assert "$After[$catalogRef] -cne $Remote" in block
    assert "refs/remotes/origin/main" not in block.split("foreach($k in $Before.Keys)", 1)[1]


def test_selector_dry_run_uses_exact_shortcut_contract_and_pid_bound_window_input() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    block = runbook.split("## Selector cancel/reload dry-run block", 1)[1].split("```powershell", 1)[1].split("```", 1)[0]

    assert "from grim_dawn_sync.shortcut import _launch_arguments" in block
    assert "$link.Arguments -cne [string]$expectedArgs[0]" in block
    assert "$link.WorkingDirectory -cne [IO.Path]::GetFullPath($sourceRoot)" in block
    assert "AppActivate" not in block and "SendKeys" not in block
    assert "GetWindowThreadProcessId" in block and "PostMessage" in block
    assert "@(SelectorWindows).Count -ne 0" in block
    assert "SendSelectorKey $ui 'ESC'" in block
    assert block.count("SendSelectorKey $ui 'F5'") == 1


def test_selector_dry_run_fails_closed_for_malicious_shortcut_and_window_ambiguity() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    block = runbook.split("## Selector cancel/reload dry-run block", 1)[1].split("```powershell", 1)[1].split("```", 1)[0]

    # Exact equality rejects appended or substituted launch arguments.  Window
    # enumeration rejects a pre-existing title, duplicate title, or ambiguous
    # new Python ownership before any ESC/F5 input is sent.
    assert "$link.Arguments -cne [string]$expectedArgs[0]" in block
    assert "if(@(SelectorWindows).Count -ne 0){throw 'precondition_failed'}" in block
    assert "$windows.Count -eq 1 -and $new.Count -eq 1" in block
    assert "$windows.Count -gt 1 -or $new.Count -gt 1" in block
    assert "if((GameRunning)){throw 'game_started_unexpectedly'}" in block


def test_selector_dry_run_ref_and_output_boundaries_are_privacy_safe() -> None:
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    block = runbook.split("## Selector cancel/reload dry-run block", 1)[1].split("```powershell", 1)[1].split("```", 1)[0]

    assert "foreach($k in $Before.Keys){if($k -cne $catalogRef" in block
    assert "foreach($k in $After.Keys){if($k -cne $catalogRef" in block
    assert "$After[$catalogRef] -cne $Remote" in block
    assert "ConvertTo-Json -Compress" in block
    assert "2>$null" in block
    assert "Write-Output (Out-DryRun" in block
