from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "ops" / "terminal-a-save-sync.ps1").read_text(encoding="utf-8")
DOC = (ROOT / "docs" / "operations" / "terminal-a-handoff.md").read_text(encoding="utf-8")


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
    assert "'-3' '-c'" in SCRIPT
    assert "sys.version_info >= (3, 11)" in SCRIPT
    assert "'-3', '-m', 'venv'" in SCRIPT
    assert "-3.11" not in SCRIPT
    assert "Python 3.11以上" in DOC


def test_terminal_a_handoff_doc_uses_placeholder_and_describes_return_sentinel() -> None:
    assert "<PRIVATE_VAULT_URL>" in DOC
    assert "TERMINAL_A_HANDOFF" in DOC
    assert "資格情報をURLに埋め込まず" in DOC
