from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "ops" / "terminal-a-save-sync.ps1").read_text(encoding="utf-8")
DOC = (ROOT / "docs" / "operations" / "terminal-a-handoff.md").read_text(encoding="utf-8")
AGENT_ENROLL_DOC = (ROOT / "docs" / "operations" / "terminal-a-agent-enroll.md").read_text(encoding="utf-8")
ROUNDTRIP_LEG1_DOC = (ROOT / "docs" / "operations" / "terminal-a-roundtrip-leg1.md").read_text(encoding="utf-8")
ROUNDTRIP_DIAGNOSE_DOC = (ROOT / "docs" / "operations" / "terminal-a-roundtrip-diagnose.md").read_text(encoding="utf-8")
TERMINAL_A_PRESERVE_DOC = (ROOT / "docs" / "operations" / "terminal-a-preserve-authoritative.md").read_text(encoding="utf-8")
TERMINAL_A_RECOVERY_DOC = (ROOT / "docs" / "operations" / "terminal-a-recover-wait-exit.md").read_text(encoding="utf-8")
TERMINAL_A_AUTHORITATIVE_SNAPSHOT_DOC = (ROOT / "docs" / "operations" / "terminal-a-authoritative-snapshot.md").read_text(encoding="utf-8")
TERMINAL_A_ROUNDTRIP_RETURN_DOC = (ROOT / "docs" / "operations" / "terminal-a-roundtrip-return.md").read_text(encoding="utf-8")
TERMINAL_A_ROUNDTRIP_RETURN_VERIFY_DOC = (ROOT / "docs" / "operations" / "terminal-a-roundtrip-return-verify.md").read_text(encoding="utf-8")


def test_terminal_a_handoff_requires_explicit_safe_inputs_and_uses_distinct_machine_id() -> None:
    assert "[Parameter(Mandatory = $true)]" in SCRIPT
    assert "[switch]$CloudDisabledConfirmed" in SCRIPT
    assert "[string]$MachineId = 'desktop-a'" in SCRIPT
    assert "machine_id_collision" in SCRIPT


def test_terminal_a_handoff_has_no_vault_url_and_blocks_unsafe_operations() -> None:
    assert "grim-dawn-save-vault" not in SCRIPT
    assert "bootstrap" not in SCRIPT
    assert "snapshot" not in SCRIPT
    assert "restore" not in SCRIPT
    assert "push" not in SCRIPT
    assert "'-m', 'grim_dawn_sync', '--config', $configPath, '--json', 'enroll'" in SCRIPT
    assert "grim_dawn_sync.cli" not in SCRIPT
    assert "'enroll', '--apply'" in SCRIPT


def test_terminal_a_handoff_checks_master_ps51_encoding_doctor_and_existing_config() -> None:
    assert "origin/master" in SCRIPT
    assert "UTF8Encoding($false)" in SCRIPT
    assert "Set-Content -LiteralPath $configPath -Encoding utf8NoBOM" not in SCRIPT
    assert "function Assert-Doctor" in SCRIPT
    assert "ConvertFrom-Json" in SCRIPT
    assert "doctor_vault_not_ready" in SCRIPT
    assert "function Assert-ExistingConfig" in SCRIPT
    assert "existing_config_mismatch" in SCRIPT
    assert "libraryfolders.vdf" in SCRIPT


def test_terminal_a_handoff_accepts_any_python_311_or_later() -> None:
    assert "@('py', 'python', 'python3')" in SCRIPT
    assert "Get-Command -Name $name -CommandType Application" in SCRIPT
    assert "import sys; print(sys.executable)" in SCRIPT
    assert "sys.version_info >= (3, 11)" in SCRIPT
    assert "Invoke-Quiet $interpreterPython @('-m', 'venv', $venvPath) 'venv_create_failed'" in SCRIPT
    assert "function Install-SourcePath" in SCRIPT
    assert "grim_dawn_sync_source.pth" in SCRIPT
    assert "'-m', 'pip', 'install'" not in SCRIPT
    assert "sysconfig.get_path(chr(112)+chr(117)+chr(114)+chr(101)+chr(108)+chr(105)+chr(98))" in SCRIPT
    assert "source_package_import_failed" in SCRIPT
    assert "-3.11" not in SCRIPT
    assert "Python 3.11以上" in DOC


def test_terminal_a_handoff_reports_sanitized_stage_failure_codes() -> None:
    assert "function Get-PythonExecutable" in SCRIPT
    assert "python_launcher_not_found" in SCRIPT
    assert "source_fetch_failed" in SCRIPT
    assert "venv_create_failed" in SCRIPT
    assert "source_path_install" in SCRIPT
    assert "source_package_missing" in SCRIPT
    assert "venv_site_packages_unavailable" in SCRIPT
    assert "source_package_import_failed" in SCRIPT
    assert "vault_clone_failed" in SCRIPT
    assert "doctor_command_failed" in SCRIPT
    assert "enroll_dry_run_failed" in SCRIPT
    assert "enroll_apply_failed" in SCRIPT
    assert "handoff_failed" not in SCRIPT
    assert "$setupStage + '_failed'" in SCRIPT


def test_terminal_a_handoff_doc_uses_placeholder_and_describes_return_sentinel() -> None:
    assert "<PRIVATE_VAULT_URL>" in DOC
    assert "TERMINAL_A_HANDOFF" in DOC
    assert "資格情報をURLに埋め込まず" in DOC


def test_terminal_a_agent_enroll_runbook_uses_existing_vault_origin_without_disclosure() -> None:
    assert "$vaultRemoteUrl = ((& git -C $vault remote get-url origin 2>$null)" in AGENT_ENROLL_DOC
    assert "-VaultRemoteUrl $vaultRemoteUrl -CloudDisabledConfirmed -ApplyEnroll" in AGENT_ENROLL_DOC
    assert "git -C $source pull --ff-only" in AGENT_ENROLL_DOC
    assert "github.com/" not in AGENT_ENROLL_DOC
    assert "<PRIVATE_VAULT_URL>" not in AGENT_ENROLL_DOC


def test_terminal_a_agent_enroll_runbook_checks_processes_and_reports_sentinel_only() -> None:
    assert "Get-Process -Name 'Grim Dawn', 'DPYes'" in AGENT_ENROLL_DOC
    assert "game_or_dpyes_running" in AGENT_ENROLL_DOC
    assert "post_enroll_doctor" in AGENT_ENROLL_DOC
    assert "--json doctor *> $null" in AGENT_ENROLL_DOC
    assert "--json status *> $null" in AGENT_ENROLL_DOC
    assert "Report that one JSON line only." in AGENT_ENROLL_DOC
    assert "Windows PowerShell 5.1" in AGENT_ENROLL_DOC


def test_terminal_a_agent_enroll_runbook_relays_canonical_blocked_sentinel() -> None:
    assert "$enrollExitCode = $LASTEXITCODE" in AGENT_ENROLL_DOC
    assert "if (-not $enrollSentinel) { throw 'enroll_apply_failed' }" in AGENT_ENROLL_DOC
    assert "$enroll.status -eq 'blocked'" in AGENT_ENROLL_DOC
    assert "Write-Output $enrollSentinel" in AGENT_ENROLL_DOC
    assert "$enrollExitCode -ne 0 -or $enroll.status -ne 'enrolled'" in AGENT_ENROLL_DOC
    assert "Enrollment never overwrites a different existing live save" in AGENT_ENROLL_DOC


def test_terminal_a_roundtrip_leg1_runbook_is_a_sanitized_ps51_launch_boundary() -> None:
    assert "Windows PowerShell 5.1" in ROUNDTRIP_LEG1_DOC
    assert "git -C $source pull --ff-only" in ROUNDTRIP_LEG1_DOC
    assert "python -m grim_dawn_sync" in ROUNDTRIP_LEG1_DOC
    assert "--json launch" in ROUNDTRIP_LEG1_DOC
    assert "TERMINAL_A_ROUNDTRIP" in ROUNDTRIP_LEG1_DOC
    assert "launch_complete" in ROUNDTRIP_LEG1_DOC
    assert "Report exactly one JSON line" in ROUNDTRIP_LEG1_DOC


def test_terminal_a_roundtrip_leg1_runbook_validates_both_status_boundaries_without_leaks() -> None:
    command = ROUNDTRIP_LEG1_DOC.split("```powershell", 1)[1].split("```", 1)[0]
    assert "$before = Get-Status 'pre_status_failed'" in ROUNDTRIP_LEG1_DOC
    assert "$after = Get-Status 'post_status_failed'" in ROUNDTRIP_LEG1_DOC
    assert "$before.readiness -ne 'ready'" in ROUNDTRIP_LEG1_DOC
    assert "$before.processes.status -ne 'clear' -or -not $before.processes.complete" in ROUNDTRIP_LEG1_DOC
    assert "$before.active_lock -ne $null -or $before.recovery_phase -ne $null" in ROUNDTRIP_LEG1_DOC
    assert "$after.vault_relation -ne 'equal'" in ROUNDTRIP_LEG1_DOC
    assert "$after.remote_commit -eq $before.remote_commit" in ROUNDTRIP_LEG1_DOC
    assert "^[0-9a-f]{40}(?:[0-9a-f]{24})?$" in ROUNDTRIP_LEG1_DOC
    assert "*> $null" in command
    assert "@($output) -join [Environment]::NewLine" in command
    assert "@($launchOutput) -join [Environment]::NewLine" in command
    assert "$status.schema_version -ne '1.0.0' -or $status.command -ne 'status'" in command
    assert "$launch.schema_version -ne '1.0.0' -or $launch.command -ne 'launch'" in command
    assert "Where-Object" not in command
    for forbidden in (" recover", " bootstrap", " snapshot", " restore", " reset", " rebase", " force", "Stop-Process"):
        assert forbidden not in command


def test_terminal_a_roundtrip_leg1_validates_machine_id_from_existing_config_not_status() -> None:
    assert "Get-Content -LiteralPath $config -Raw -Encoding utf8 | ConvertFrom-Json" in ROUNDTRIP_LEG1_DOC
    assert "$existingConfig.machine_id -ne $machineId" in ROUNDTRIP_LEG1_DOC
    assert "existing_config_mismatch" in ROUNDTRIP_LEG1_DOC
    assert "$before.machine_id" not in ROUNDTRIP_LEG1_DOC
    assert "$after.machine_id" not in ROUNDTRIP_LEG1_DOC


def test_terminal_a_roundtrip_leg1_accepts_only_the_expected_stale_a_boundary() -> None:
    assert "$before.vault_relation -eq 'equal'" in ROUNDTRIP_LEG1_DOC
    assert "$before.vault_relation -eq 'remote_changed_or_unknown'" in ROUNDTRIP_LEG1_DOC
    assert "$before.readiness -ne 'blocked'" in ROUNDTRIP_LEG1_DOC
    assert "$before.last_pushed_commit -match '^[0-9a-f]{40}(?:[0-9a-f]{24})?$'" in ROUNDTRIP_LEG1_DOC
    assert "$before.last_pushed_commit -eq $before.remote_commit" in ROUNDTRIP_LEG1_DOC
    assert "preflight_remote_state_inconsistent" in ROUNDTRIP_LEG1_DOC
    assert "launch workflow itself performs the authoritative fetch and reconciliation" in ROUNDTRIP_LEG1_DOC


def test_terminal_a_roundtrip_leg1_requires_known_b_save_property_before_a_change() -> None:
    assert "ask the user to name one known property that identifies B's newer save" in ROUNDTRIP_LEG1_DOC
    assert "such as recent progress, an inventory item, or character position" in ROUNDTRIP_LEG1_DOC
    assert "first verify the named B-save property on A" in ROUNDTRIP_LEG1_DOC
    assert "Do not make or save a new change until that property is visibly confirmed" in ROUNDTRIP_LEG1_DOC
    assert "If the user does not identify a known B-save property" in ROUNDTRIP_LEG1_DOC
    assert '"code":"b_save_not_visible"' in ROUNDTRIP_LEG1_DOC
    assert "do not launch again" in ROUNDTRIP_LEG1_DOC


def test_terminal_a_roundtrip_diagnosis_is_ps51_read_only_and_sanitized() -> None:
    assert "Windows PowerShell 5.1" in ROUNDTRIP_DIAGNOSE_DOC
    assert "TERMINAL_A_DIAGNOSIS" in ROUNDTRIP_DIAGNOSE_DOC
    assert "--json status" in ROUNDTRIP_DIAGNOSE_DOC
    assert "--json doctor" in ROUNDTRIP_DIAGNOSE_DOC
    assert "git -C $source pull" not in ROUNDTRIP_DIAGNOSE_DOC
    assert "--json launch" not in ROUNDTRIP_DIAGNOSE_DOC
    assert "--json recover" not in ROUNDTRIP_DIAGNOSE_DOC
    assert "Stop-Process" not in ROUNDTRIP_DIAGNOSE_DOC


def test_terminal_a_roundtrip_diagnosis_parses_known_schema_and_allowlists_output() -> None:
    command = ROUNDTRIP_DIAGNOSE_DOC.split("```powershell", 1)[1].split("```", 1)[0]
    assert "$existingConfig.machine_id -ne $machineId" in command
    assert "function Get-StatusOrThrow" in command
    assert "throw 'status_command_failed'" in command
    assert "throw 'status_parse_failed'" in command
    assert "throw 'status_shape_invalid'" in command
    assert "function Get-DoctorOptional" in command
    assert "Get-LaunchFailure" in command
    assert "'grim-dawn-sync recover'" in command
    assert "'grim-dawn-sync status'" in command
    assert "ConvertTo-Json -Compress" in command
    assert "Get-Content -LiteralPath $LogPath -Encoding utf8 -ErrorAction Stop" in command
    assert "$recoveryRequired = ($lockPresent -or $status.recovery_phase -ne $null -or $readiness -eq 'recovery_required')" in command
    assert "$statusOut = 'diagnosed'; $code = 'diagnosis_complete'" in command
    assert "[void](Get-DoctorOptional)" in command
    assert "[System.IO.Path]::GetDirectoryName($config)" in command
    assert "Split-Path -LiteralPath $config -Parent" not in command
    for forbidden in ("remote_commit", "last_pushed_commit", "safe_oid", "safe_root_hash", "session_id", "archive_id"):
        assert forbidden not in command


def test_terminal_a_preserve_authoritative_runbook_is_ps51_sanitized_and_state_preserving() -> None:
    command = TERMINAL_A_PRESERVE_DOC.split("```powershell", 1)[1].split("```", 1)[0]

    assert "Windows PowerShell 5.1" in TERMINAL_A_PRESERVE_DOC
    assert "git -C $source pull --ff-only" in command
    assert "--json', 'preserve'" in command
    assert "'--apply'" in command
    assert "TERMINAL_A_PRESERVE" in command
    assert "live_archive_verified" in command
    assert "ConvertFrom-Json -ErrorAction Stop" in command
    assert "ConvertTo-Json -Compress" in command
    assert "$before.processes.status -ne 'clear'" in command
    assert "$before.processes.complete -ne $true" in command
    assert "-not $beforeLockPresent -or -not $beforeRecoveryRequired" in command
    assert "$afterLockPresent -ne $beforeLockPresent" in command
    assert "$afterRecoveryRequired -ne $beforeRecoveryRequired" in command
    assert "$afterRecoveryPhasePresent -ne $beforeRecoveryPhasePresent" in command
    assert "^save-preserved-[0-9a-f]{16}-[0-9a-f]{32}$" in command
    assert "archive_id =" not in command
    assert "root_hash" not in command
    assert "file_count" not in command
    assert "character_count" not in command
    assert "vaultRemoteUrl" not in command
    for forbidden in (
        "--json launch", "--json recover", "--json snapshot", "--json restore",
        "--json bootstrap", " push", "Stop-Process", "Remove-Item",
    ):
        assert forbidden not in command


def test_terminal_a_wait_exit_recovery_runbook_is_single_use_and_preserves_live_and_main() -> None:
    command = TERMINAL_A_RECOVERY_DOC.split("```powershell", 1)[1].split("```", 1)[0]

    assert "Windows PowerShell 5.1" in TERMINAL_A_RECOVERY_DOC
    assert "git -C $source pull --ff-only" in command
    assert "TERMINAL_A_RECOVERY" in command
    assert "lock_released_live_untouched" in command
    assert "--json recover" in command
    assert command.count("--json recover") == 1
    assert "$recovery.result -ne 'abandoned_lock_released'" in command
    assert "$before.recovery_phase -ne 'lock_held'" in command
    assert "$before.active_lock.machine_id -ne $machineId" in command
    assert "$after.active_lock -ne $null" in command
    assert "$after.recovery_phase -ne $null" in command
    assert "$after.remote_commit -ne $beforeRemote" in command
    assert "$afterDoctor.checks.save_root.manifest.root_hash -ne $beforeLiveRoot" in command
    assert "$after.last_pushed_commit -ne $beforeRemote" in command
    assert "ConvertTo-Json -Compress" in command
    for forbidden in (
        "--json launch", "--json snapshot", "--json restore", "--json bootstrap",
        " push", "Stop-Process", "Remove-Item", "git -C $source reset",
    ):
        assert forbidden not in command


def test_terminal_a_authoritative_snapshot_runbook_proves_and_publishes_only_newer_live() -> None:
    command = TERMINAL_A_AUTHORITATIVE_SNAPSHOT_DOC.split("```powershell", 1)[1].split("```", 1)[0]

    assert "Windows PowerShell 5.1" in TERMINAL_A_AUTHORITATIVE_SNAPSHOT_DOC
    assert "git -C $source pull --ff-only" in command
    assert "TERMINAL_A_AUTHORITATIVE" in command
    assert "snapshot_pushed_live_preserved" in command
    assert command.count("--json snapshot") == 1
    assert command.count("--json restore") == 1
    assert "Get-RestoreOrThrow 'pre' $beforeRemote" in command
    assert "Get-RestoreOrThrow 'post' $snapshot.commit" in command
    assert "function Get-DoctorOrThrow" in command
    assert "function Get-RestoreOrThrow" in command
    assert "$before.active_lock -ne $null" in command
    assert "$before.recovery_phase -ne $null" in command
    assert "$before.readiness -ne 'ready'" in command
    assert "$before.vault_relation -ne 'equal'" in command
    assert "$beforeRestore.root_hash -eq $beforeLiveRoot" in command
    assert "$snapshot.root_hash -ne $beforeLiveRoot" in command
    assert "$snapshot.commit -eq $beforeRemote" in command
    assert "$after.remote_commit -ne $snapshot.commit" in command
    assert "$after.last_pushed_commit -ne $snapshot.commit" in command
    assert "$afterDoctor.checks.save_root.manifest.root_hash -ne $beforeLiveRoot" in command
    assert "$afterRestore.root_hash -ne $beforeLiveRoot" in command
    assert "ConvertTo-Json -Compress" in command
    for forbidden in (
        "--json launch", "--json recover", "--json bootstrap", "--json enroll",
        "--apply", " push", "Stop-Process", "Remove-Item", "git -C $source reset", " clean",
    ):
        assert forbidden not in command


def test_terminal_a_roundtrip_return_runbook_requires_stale_unchanged_a_and_verifies_b_return() -> None:
    command = TERMINAL_A_ROUNDTRIP_RETURN_DOC.split("```powershell", 1)[1].split("```", 1)[0]

    assert "Windows PowerShell 5.1" in TERMINAL_A_ROUNDTRIP_RETURN_DOC
    assert "git -C $source pull --ff-only" in command
    assert "TERMINAL_A_ROUNDTRIP" in command
    assert "A_RETURN" in command
    assert "roundtrip_complete" in command
    assert command.count("--json launch") == 1
    assert command.count("--json restore") == 1
    assert "Get-RestoreOrThrow 'pre' $oldBaseline" in command
    assert "Get-RestoreOrThrow 'post' $launch.result.commit" in command
    assert "$before.readiness -ne 'blocked'" in command
    assert "$before.vault_relation -ne 'remote_changed_or_unknown'" in command
    assert "$before.remote_commit -eq $before.last_pushed_commit" in command
    assert "$oldRestore.root_hash -ne $beforeLiveRoot" in command
    assert "$launch.result.commit -eq $beforeRemote" in command
    assert "$after.remote_commit -eq $beforeRemote" in command
    assert "$after.remote_commit -ne $launch.result.commit" in command
    assert "$after.last_pushed_commit -ne $launch.result.commit" in command
    assert "$newRestore.root_hash -ne $afterLiveRoot" in command
    assert "ConvertTo-Json -Compress" in command
    for forbidden in (
        "--json recover", "--json snapshot", "--apply", "--json bootstrap", "--json enroll",
        " push", "Stop-Process", "Remove-Item", "git -C $source reset", " clean",
    ):
        assert forbidden not in command


def test_terminal_a_return_postflight_allows_dpyes_and_has_readonly_verify_runbook() -> None:
    command = TERMINAL_A_ROUNDTRIP_RETURN_DOC.split("```powershell", 1)[1].split("```", 1)[0]
    verify = TERMINAL_A_ROUNDTRIP_RETURN_VERIFY_DOC.split("```powershell", 1)[1].split("```", 1)[0]
    assert "Get-DoctorOrThrow 'post' $true" in command
    assert "Assert-GrimDawnNotRunning 'post'" in command
    assert "Get-Process -Name 'Grim Dawn'" in command
    assert "A_RETURN_VERIFY" in verify
    assert "roundtrip_verified" in verify
    assert "@('status')" in verify and "@('doctor')" in verify and "@('restore','--commit',$commit)" in verify
    assert "Assert-LastLaunchComplete $commit" in verify
    for forbidden in ("--json launch", "--json snapshot", "--json recover", "--apply", " push", "Stop-Process", "Remove-Item"):
        assert forbidden not in verify
