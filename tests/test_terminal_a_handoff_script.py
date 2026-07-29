from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "ops" / "terminal-a-save-sync.ps1").read_text(encoding="utf-8")
DOC = (ROOT / "docs" / "operations" / "terminal-a-handoff.md").read_text(encoding="utf-8")
AGENT_ENROLL_DOC = (ROOT / "docs" / "operations" / "terminal-a-agent-enroll.md").read_text(encoding="utf-8")


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
