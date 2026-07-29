# Terminal A: agent-run enrollment

This runbook is for an agent working directly on Terminal A. The prerequisite setup and doctor check have already passed for machine ID `desktop-a`. The only intended state-changing action is enrollment: adopt the verified remote snapshot. Enrollment never overwrites a different existing live save; a mismatch remains blocked.

The agent must not print or relay command output, paths, remote values, save names, hashes, or diagnostics. Its final report is exactly one `TERMINAL_A_HANDOFF` JSON sentinel.

## Boundaries

- Use the existing public source checkout at `$env:USERPROFILE\grimdawnrep`.
- Read the private Vault remote only from the existing Vault's `origin`; never write, display, log, or otherwise relay that value.
- First update public source with `git pull --ff-only`.
- If Grim Dawn or DPYes is running, do not stop either process. Report the blocked sentinel and make no enrollment attempt.
- Do not bootstrap, push, snapshot, restore, launch the game, kill processes, edit source, commit, or perform a repair/cleanup action.
- On any failure, do not improvise a destructive repair. Report only its sentinel code and stage.

## Copy-paste execution (Windows PowerShell 5.1)

Run the following in **Windows PowerShell 5.1**, not PowerShell 7. It suppresses all command output and prints one sanitized sentinel line at the end.

```powershell
$source = Join-Path $env:USERPROFILE 'grimdawnrep'
$vault = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveVault'
$machineId = 'desktop-a'
$stage = 'source_pull'

function Write-Handoff([string]$Status, [string]$Code) {
    [ordered]@{ sentinel = 'TERMINAL_A_HANDOFF'; status = $Status; code = $Code; machine_id = $machineId } |
        ConvertTo-Json -Compress
}

try {
    & git -C $source pull --ff-only *> $null
    if ($LASTEXITCODE -ne 0) { throw 'source_pull_failed' }

    $stage = 'vault_origin'
    if (-not (Test-Path -LiteralPath $vault -PathType Container)) { throw 'vault_missing' }
    $vaultRemoteUrl = ((& git -C $vault remote get-url origin 2>$null) -join [Environment]::NewLine).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $vaultRemoteUrl) { throw 'vault_origin_unavailable' }

    $stage = 'process_check'
    $running = Get-Process -Name 'Grim Dawn', 'DPYes' -ErrorAction SilentlyContinue
    if ($running) {
        Write-Output (Write-Handoff 'blocked' 'game_or_dpyes_running')
        exit 1
    }

    $stage = 'enroll_apply'
    $enrollOutput = & (Join-Path $source 'ops\terminal-a-save-sync.ps1') `
        -VaultRemoteUrl $vaultRemoteUrl -CloudDisabledConfirmed -ApplyEnroll 2>$null
    $enrollExitCode = $LASTEXITCODE
    $enrollSentinel = @($enrollOutput | Where-Object { $_ -match '^\{"sentinel":"TERMINAL_A_HANDOFF"' }) | Select-Object -Last 1
    if (-not $enrollSentinel) { throw 'enroll_apply_failed' }
    $enroll = $enrollSentinel | ConvertFrom-Json -ErrorAction Stop
    if ($enroll.sentinel -ne 'TERMINAL_A_HANDOFF' -or $enroll.machine_id -ne $machineId -or
        $enroll.code -notmatch '^[a-z0-9_]+$') { throw 'enroll_apply_failed' }
    if ($enroll.status -eq 'blocked') {
        Write-Output $enrollSentinel
        exit 1
    }
    if ($enrollExitCode -ne 0 -or $enroll.status -ne 'enrolled' -or $enroll.code -ne 'enroll_apply_passed') {
        throw 'enroll_apply_failed'
    }

    $stage = 'post_enroll_doctor'
    $python = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool\.venv\Scripts\python.exe'
    $config = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSync\config.local.json'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'post_enroll_python_missing' }
    & $python -m grim_dawn_sync --config $config --json doctor *> $null
    if ($LASTEXITCODE -ne 0) { throw 'post_enroll_doctor_failed' }
    $stage = 'post_enroll_status'
    & $python -m grim_dawn_sync --config $config --json status *> $null
    if ($LASTEXITCODE -ne 0) { throw 'post_enroll_status_failed' }

    Write-Output (Write-Handoff 'enrolled' 'enroll_apply_passed')
    exit 0
}
catch {
    $code = if ($_.Exception.Message -match '^[a-z0-9_]+$') { $_.Exception.Message } else { $stage + '_failed' }
    Write-Output (Write-Handoff 'blocked' $code)
    exit 1
}
```

Report that one JSON line only. A successful result is:

```json
{"sentinel":"TERMINAL_A_HANDOFF","status":"enrolled","code":"enroll_apply_passed","machine_id":"desktop-a"}
```
