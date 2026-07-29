# Terminal A roundtrip: leg A1 launch

This runbook performs the first leg of the A -> B -> A acceptance test on the already enrolled Terminal A (`desktop-a`). It uses the existing checkout, virtual environment, configuration, and Vault. It does not disclose or modify their paths, remote URL, save name, hashes, or command output.

## Required user coordination

Before running the PowerShell block, the agent must ask the user to name one known property that identifies B's newer save, such as recent progress, an inventory item, or character position. Record only the user's description in the conversation, never any path, hash, or raw save data. Then tell the user: **the block will first reconcile Terminal A with Terminal B's newer authoritative save and then open DPYes/Grim Dawn. When the game opens, first verify the named B-save property on A. Do not make or save a new change until that property is visibly confirmed. Tell the agent whether it is present. If it is present, make one short, unmistakable A-side change (for example pick up an item or change character position), save normally, and exit the game normally. If it is absent, make no deliberate change, do not save, and exit normally. Do not start another game instance.** Wait for the user to name the property and acknowledge this instruction. The CLI below is the only permitted game launch method.

If the user does not identify a known B-save property and acknowledge the instructions, stop without running anything. The agent must not kill a process, retry launch, or attempt recovery. When the game closes, the command waits for the save to stabilize and completes its normal protected workflow. Because the script cannot observe the game screen, the agent must obtain the user's explicit visual result. If the user reports that the named B-save property was absent, the leg failed regardless of the command result: do not launch again and report only `{"sentinel":"TERMINAL_A_ROUNDTRIP","status":"blocked","leg":"A1","machine_id":"desktop-a","code":"b_save_not_visible"}`.

## Boundaries

- Use **Windows PowerShell 5.1**, not PowerShell 7.
- Update only the public source with fast-forward-only pull. Use the existing virtual environment and existing local configuration; do not bootstrap, repair, edit source, commit, or change configuration.
- Do not manually push, snapshot, restore, recover, reset, rebase, force, or terminate a process. The launch CLI's own necessary lock, restore, snapshot, and push operations are the sole exception.
- Do not print or relay paths, URLs, save names, hashes, commits, or raw command output. A failure is a fixed, sanitized stage code; do not improvise a remedy.

## Copy-paste execution

Run this block only after the required user acknowledgement. It suppresses all command output and emits one final sentinel. It requires a complete, clear process scan with no active lock or recovery before launch. The expected starting state may be either equal, or `remote_changed_or_unknown` because B advanced the remote while A still records its older applied commit. The launch workflow itself performs the authoritative fetch and reconciliation, and refuses an unsafe ahead or diverged relation. After launch the block additionally requires that the remote commit changed and the Vault is equal again.

```powershell
$source = Join-Path $env:USERPROFILE 'grimdawnrep'
$toolRoot = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool'
$python = Join-Path $toolRoot '.venv\Scripts\python.exe'
$config = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSync\config.local.json'
$machineId = 'desktop-a'
$stage = 'source_pull_failed'

function Write-Roundtrip([string]$Status, [string]$Code) {
    [ordered]@{ sentinel = 'TERMINAL_A_ROUNDTRIP'; status = $Status; leg = 'A1'; machine_id = $machineId; code = $Code } |
        ConvertTo-Json -Compress
}

function Get-Status([string]$FailureCode) {
    $output = @(& $python -m grim_dawn_sync --config $config --json status 2>$null)
    $exitCode = $LASTEXITCODE
    $json = @($output) -join [Environment]::NewLine
    if ($exitCode -ne 0 -or -not $json) { throw $FailureCode }
    try { $status = $json | ConvertFrom-Json -ErrorAction Stop }
    catch { throw $FailureCode }
    if ($status.schema_version -ne '1.0.0' -or $status.command -ne 'status') { throw $FailureCode }
    return $status
}

try {
    & git -C $source pull --ff-only *> $null
    if ($LASTEXITCODE -ne 0) { throw 'source_pull_failed' }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $config -PathType Leaf)) {
        throw 'existing_environment_missing'
    }
    try { $existingConfig = Get-Content -LiteralPath $config -Raw -Encoding utf8 | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'existing_config_invalid' }
    if ($existingConfig.machine_id -ne $machineId) { throw 'existing_config_mismatch' }

    $before = Get-Status 'pre_status_failed'
    if ($before.processes.status -ne 'clear' -or -not $before.processes.complete -or
        $before.active_lock -ne $null -or $before.recovery_phase -ne $null -or
        -not ($before.remote_commit -match '^[0-9a-f]{40}(?:[0-9a-f]{24})?$')) {
        throw 'preflight_not_ready'
    }
    if ($before.vault_relation -eq 'equal') {
        if ($before.readiness -ne 'ready') { throw 'preflight_not_ready' }
    } elseif ($before.vault_relation -eq 'remote_changed_or_unknown') {
        if ($before.readiness -ne 'blocked' -or
            -not ($before.last_pushed_commit -match '^[0-9a-f]{40}(?:[0-9a-f]{24})?$') -or
            $before.last_pushed_commit -eq $before.remote_commit) {
            throw 'preflight_remote_state_inconsistent'
        }
    } else {
        throw 'preflight_not_ready'
    }

    $stage = 'launch_failed'
    $launchOutput = @(& $python -m grim_dawn_sync --config $config --json launch 2>$null)
    $launchExitCode = $LASTEXITCODE
    $launchJson = @($launchOutput) -join [Environment]::NewLine
    if ($launchExitCode -ne 0 -or -not $launchJson) { throw 'launch_failed' }
    try { $launch = $launchJson | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'launch_result_invalid' }
    if ($launch.schema_version -ne '1.0.0' -or $launch.command -ne 'launch' -or
        $launch.result.state -ne 'COMPLETE' -or -not ($launch.result.commit -match '^[0-9a-f]{40}(?:[0-9a-f]{24})?$')) {
        throw 'launch_result_invalid'
    }

    $after = Get-Status 'post_status_failed'
    if ($after.readiness -ne 'ready' -or
        $after.processes.status -ne 'clear' -or -not $after.processes.complete -or
        $after.active_lock -ne $null -or $after.recovery_phase -ne $null -or
        $after.vault_relation -ne 'equal' -or -not ($after.remote_commit -match '^[0-9a-f]{40}(?:[0-9a-f]{24})?$') -or
        $after.remote_commit -eq $before.remote_commit -or $after.remote_commit -ne $launch.result.commit) {
        throw 'postflight_verification_failed'
    }

    Write-Output (Write-Roundtrip 'leg_complete' 'launch_complete')
    exit 0
}
catch {
    $code = if ($_.Exception.Message -match '^(source_pull_failed|existing_environment_missing|existing_config_invalid|existing_config_mismatch|pre_status_failed|preflight_not_ready|preflight_remote_state_inconsistent|launch_failed|launch_result_invalid|post_status_failed|postflight_verification_failed)$') {
        $_.Exception.Message
    } else {
        $stage
    }
    Write-Output (Write-Roundtrip 'blocked' $code)
    exit 1
}
```

Report exactly one JSON line: either the final command output or the corresponding blocked sentinel. A successful A1 result is:

```json
{"sentinel":"TERMINAL_A_ROUNDTRIP","status":"leg_complete","leg":"A1","machine_id":"desktop-a","code":"launch_complete"}
```
