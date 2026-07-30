# Terminal A: release the pre-snapshot WAIT_GAME_EXIT lock

Use this runbook only for the already preserved Terminal A (`desktop-a`) state:
the diagnosis reported `game_identity_unknown` at `WAIT_GAME_EXIT`, both
processes are clear, the active lock is owned by A, the recovery phase is
`lock_held`, and the vault relation is `equal`.  The verified preserve archive
must already exist.

This is deliberately a narrow recovery.  It releases only a session proven to
have stopped before a vault commit: its local vault must be clean, attached to
the configured branch, and exactly at the verified remote base.  It does not
read or write the live save.  The remote main commit is retained; only the
matching remote session-lock ref is released.  The post-check compares the
live manifest root and remote commit internally, without printing either.

## Boundaries

- Run this in **Windows PowerShell 5.1**, not PowerShell 7.
- The block pulls the public source repository once, then runs exactly one
  `recover` command.  Do not run the block a second time.
- Do not launch, snapshot, restore, bootstrap, manually push, delete, clean
  up, repair, or edit configuration.  Do not stop processes.
- It emits one sanitized JSON line only.  Do not relay command output, paths,
  hashes, session IDs, or remote details.
- A blocked result means that the observed state no longer exactly matches this
  runbook.  Stop and wait for a new instruction.

## Copy-paste execution

```powershell
$source = 'C:\Users\melof\grimdawnrep'
$toolRoot = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool'
$python = Join-Path $toolRoot '.venv\Scripts\python.exe'
$config = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSync\config.local.json'
$machineId = 'desktop-a'
$allowedCodes = @(
    'source_update_failed', 'environment_check_failed', 'config_check_failed',
    'pre_status_failed', 'pre_status_parse_failed', 'pre_status_shape_invalid',
    'preconditions_changed', 'pre_doctor_failed', 'pre_doctor_parse_failed',
    'pre_doctor_shape_invalid', 'recover_failed', 'recover_parse_failed',
    'recover_shape_invalid', 'recover_result_unexpected', 'post_status_failed',
    'post_status_parse_failed', 'post_status_shape_invalid',
    'post_doctor_failed', 'post_doctor_parse_failed', 'post_doctor_shape_invalid',
    'postconditions_changed', 'unexpected_failed'
)

function Write-Sentinel([string]$Status, [string]$Code) {
    [ordered]@{
        sentinel = 'TERMINAL_A_RECOVERY'
        status = $Status
        code = $Code
        machine_id = $machineId
    } | ConvertTo-Json -Compress
}

function Get-StatusOrThrow([string]$Prefix) {
    $output = @(& $python -m grim_dawn_sync --config $config --json status 2>$null)
    $exitCode = $LASTEXITCODE
    $raw = @($output) -join [Environment]::NewLine
    if ($exitCode -ne 0 -or -not $raw) { throw ($Prefix + '_status_failed') }
    try { $status = $raw | ConvertFrom-Json -ErrorAction Stop }
    catch { throw ($Prefix + '_status_parse_failed') }
    if ($status.schema_version -ne '1.0.0' -or $status.command -ne 'status') {
        throw ($Prefix + '_status_shape_invalid')
    }
    return $status
}

function Get-DoctorOrThrow([string]$Prefix) {
    $output = @(& $python -m grim_dawn_sync --config $config --json doctor 2>$null)
    $exitCode = $LASTEXITCODE
    $raw = @($output) -join [Environment]::NewLine
    if ($exitCode -ne 0 -or -not $raw) { throw ($Prefix + '_doctor_failed') }
    try { $doctor = $raw | ConvertFrom-Json -ErrorAction Stop }
    catch { throw ($Prefix + '_doctor_parse_failed') }
    if ($doctor.schema_version -ne '1.0.0' -or $doctor.command -ne 'doctor' -or
        $doctor.read_only -ne $true -or $doctor.machine_id -ne $machineId -or
        $doctor.checks.processes.status -ne 'clear' -or $doctor.checks.processes.complete -ne $true -or
        $doctor.checks.save_root.manifest.root_hash -notmatch '^[0-9a-f]{64}$') {
        throw ($Prefix + '_doctor_shape_invalid')
    }
    return $doctor
}

function Invoke-RecoverOnceOrThrow {
    $output = @(& $python -m grim_dawn_sync --config $config --json recover 2>$null)
    $exitCode = $LASTEXITCODE
    $raw = @($output) -join [Environment]::NewLine
    if ($exitCode -ne 0 -or -not $raw) { throw 'recover_failed' }
    try { $recovery = $raw | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'recover_parse_failed' }
    if ($recovery.schema_version -ne '1.0.0' -or $recovery.command -ne 'recover') {
        throw 'recover_shape_invalid'
    }
    if ($recovery.result -ne 'abandoned_lock_released') { throw 'recover_result_unexpected' }
}

$resultStatus = 'blocked'
$resultCode = 'unexpected_failed'
try {
    if (-not (Test-Path -LiteralPath $source -PathType Container)) { throw 'source_update_failed' }
    & git -C $source pull --ff-only *> $null
    if ($LASTEXITCODE -ne 0) { throw 'source_update_failed' }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
        -not (Test-Path -LiteralPath $config -PathType Leaf)) { throw 'environment_check_failed' }
    try { $existingConfig = Get-Content -LiteralPath $config -Raw -Encoding utf8 | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'config_check_failed' }
    if ($existingConfig.machine_id -ne $machineId) { throw 'config_check_failed' }

    $before = Get-StatusOrThrow 'pre'
    $beforeDoctor = Get-DoctorOrThrow 'pre'
    if ($before.processes.status -ne 'clear' -or $before.processes.complete -ne $true -or
        $before.readiness -ne 'recovery_required' -or $before.vault_relation -ne 'equal' -or
        $before.active_lock -eq $null -or $before.active_lock.machine_id -ne $machineId -or
        $before.recovery_phase -ne 'lock_held' -or
        $before.remote_commit -notmatch '^[0-9a-f]{40}(?:[0-9a-f]{24})?$') {
        throw 'preconditions_changed'
    }
    $beforeRemote = [string]$before.remote_commit
    $beforeLiveRoot = [string]$beforeDoctor.checks.save_root.manifest.root_hash

    Invoke-RecoverOnceOrThrow

    $after = Get-StatusOrThrow 'post'
    $afterDoctor = Get-DoctorOrThrow 'post'
    if ($after.processes.status -ne 'clear' -or $after.processes.complete -ne $true -or
        $after.active_lock -ne $null -or $after.recovery_phase -ne $null -or
        $after.readiness -ne 'ready' -or $after.vault_relation -ne 'equal' -or
        $after.remote_commit -ne $beforeRemote -or
        $after.last_pushed_commit -ne $beforeRemote -or
        $afterDoctor.checks.save_root.manifest.root_hash -ne $beforeLiveRoot) {
        throw 'postconditions_changed'
    }

    $resultStatus = 'complete'; $resultCode = 'lock_released_live_untouched'
}
catch {
    if ($_.Exception.Message -in $allowedCodes) { $resultCode = $_.Exception.Message }
}

Write-Output (Write-Sentinel $resultStatus $resultCode)
exit $(if ($resultStatus -eq 'complete') { 0 } else { 1 })
```

Report exactly the one JSON line printed by the block.
