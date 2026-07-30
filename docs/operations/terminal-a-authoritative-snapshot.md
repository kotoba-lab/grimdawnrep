# Terminal A: publish the preserved authoritative live save

Use this runbook only after Terminal A (`desktop-a`) has completed the narrow
recovery and its currently live save is known to be newer than the vault.
It first proves that the existing remote snapshot is different, then runs one
and only one snapshot.  The snapshot operation makes a verified local archive
before it changes the vault or remote.

## Boundaries

- Run in **Windows PowerShell 5.1**, not PowerShell 7.
- Run the block exactly once.  A blocked result means its exact precondition
  changed; stop for a fresh instruction.
- No game or DPYes process may be running.
- The only state-changing save-sync command in the block is `snapshot`.
- Do not launch, recover, bootstrap, enroll, restore with `--apply`, manually
  push, reset, clean, delete files, or stop processes.
- It prints exactly one sanitized JSON line.  Do not relay paths, commit IDs,
  roots, session IDs, or other command output.

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
    'pre_doctor_shape_invalid', 'pre_restore_failed', 'pre_restore_parse_failed',
    'pre_restore_shape_invalid', 'remote_not_older', 'snapshot_failed',
    'snapshot_parse_failed', 'snapshot_shape_invalid', 'snapshot_result_unexpected',
    'post_status_failed', 'post_status_parse_failed', 'post_status_shape_invalid',
    'postconditions_changed', 'post_doctor_failed', 'post_doctor_parse_failed',
    'post_doctor_shape_invalid', 'post_restore_failed', 'post_restore_parse_failed',
    'post_restore_shape_invalid', 'unexpected_failed'
)

function Write-Sentinel([string]$Status, [string]$Code) {
    [ordered]@{ sentinel = 'TERMINAL_A_AUTHORITATIVE'; status = $Status; code = $Code; machine_id = $machineId } |
        ConvertTo-Json -Compress
}

function Get-StatusOrThrow([string]$Prefix) {
    $output = @(& $python -m grim_dawn_sync --config $config --json status 2>$null)
    $exitCode = $LASTEXITCODE; $raw = @($output) -join [Environment]::NewLine
    if ($exitCode -ne 0 -or -not $raw) { throw ($Prefix + '_status_failed') }
    try { $value = $raw | ConvertFrom-Json -ErrorAction Stop } catch { throw ($Prefix + '_status_parse_failed') }
    if ($value.schema_version -ne '1.0.0' -or $value.command -ne 'status') { throw ($Prefix + '_status_shape_invalid') }
    return $value
}

function Get-DoctorOrThrow([string]$Prefix) {
    $output = @(& $python -m grim_dawn_sync --config $config --json doctor 2>$null)
    $exitCode = $LASTEXITCODE; $raw = @($output) -join [Environment]::NewLine
    if ($exitCode -ne 0 -or -not $raw) { throw ($Prefix + '_doctor_failed') }
    try { $value = $raw | ConvertFrom-Json -ErrorAction Stop } catch { throw ($Prefix + '_doctor_parse_failed') }
    if ($value.schema_version -ne '1.0.0' -or $value.command -ne 'doctor' -or $value.read_only -ne $true -or
        $value.machine_id -ne $machineId -or $value.checks.processes.status -ne 'clear' -or
        $value.checks.processes.complete -ne $true -or $value.checks.save_root.manifest.root_hash -notmatch '^[0-9a-f]{64}$') {
        throw ($Prefix + '_doctor_shape_invalid')
    }
    return $value
}

function Get-RestoreOrThrow([string]$Prefix, [string]$Commit) {
    $output = @(& $python -m grim_dawn_sync --config $config --json restore --commit $Commit 2>$null)
    $exitCode = $LASTEXITCODE; $raw = @($output) -join [Environment]::NewLine
    if ($exitCode -ne 0 -or -not $raw) { throw ($Prefix + '_restore_failed') }
    try { $value = $raw | ConvertFrom-Json -ErrorAction Stop } catch { throw ($Prefix + '_restore_parse_failed') }
    if ($value.schema_version -ne '1.0.0' -or $value.command -ne 'restore' -or $value.commit -ne $Commit -or
        $value.dry_run -ne $true -or $value.root_hash -notmatch '^[0-9a-f]{64}$') { throw ($Prefix + '_restore_shape_invalid') }
    return $value
}

function Invoke-SnapshotOnceOrThrow {
    $output = @(& $python -m grim_dawn_sync --config $config --json snapshot 2>$null)
    $exitCode = $LASTEXITCODE; $raw = @($output) -join [Environment]::NewLine
    if ($exitCode -ne 0 -or -not $raw) { throw 'snapshot_failed' }
    try { $value = $raw | ConvertFrom-Json -ErrorAction Stop } catch { throw 'snapshot_parse_failed' }
    if ($value.schema_version -ne '1.0.0' -or $value.command -ne 'snapshot' -or
        $value.commit -notmatch '^[0-9a-f]{40}(?:[0-9a-f]{24})?$' -or $value.root_hash -notmatch '^[0-9a-f]{64}$') {
        throw 'snapshot_shape_invalid'
    }
    return $value
}

$resultStatus = 'blocked'; $resultCode = 'unexpected_failed'
try {
    if (-not (Test-Path -LiteralPath $source -PathType Container)) { throw 'source_update_failed' }
    & git -C $source pull --ff-only *> $null
    if ($LASTEXITCODE -ne 0) { throw 'source_update_failed' }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $config -PathType Leaf)) {
        throw 'environment_check_failed'
    }
    try { $existingConfig = Get-Content -LiteralPath $config -Raw -Encoding utf8 | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'config_check_failed' }
    if ($existingConfig.machine_id -ne $machineId) { throw 'config_check_failed' }

    $before = Get-StatusOrThrow 'pre'
    $beforeDoctor = Get-DoctorOrThrow 'pre'
    if ($before.processes.status -ne 'clear' -or $before.processes.complete -ne $true -or
        $before.active_lock -ne $null -or $before.recovery_phase -ne $null -or $before.readiness -ne 'ready' -or
        $before.vault_relation -ne 'equal' -or $before.remote_commit -notmatch '^[0-9a-f]{40}(?:[0-9a-f]{24})?$' -or
        $before.last_pushed_commit -ne $before.remote_commit) { throw 'preconditions_changed' }
    $beforeRemote = [string]$before.remote_commit
    $beforeLiveRoot = [string]$beforeDoctor.checks.save_root.manifest.root_hash
    $beforeRestore = Get-RestoreOrThrow 'pre' $beforeRemote
    if ($beforeRestore.root_hash -eq $beforeLiveRoot) { throw 'remote_not_older' }

    $snapshot = Invoke-SnapshotOnceOrThrow
    if ($snapshot.root_hash -ne $beforeLiveRoot -or $snapshot.commit -eq $beforeRemote) { throw 'snapshot_result_unexpected' }

    $after = Get-StatusOrThrow 'post'
    $afterDoctor = Get-DoctorOrThrow 'post'
    if ($after.processes.status -ne 'clear' -or $after.processes.complete -ne $true -or
        $after.active_lock -ne $null -or $after.recovery_phase -ne $null -or $after.readiness -ne 'ready' -or
        $after.vault_relation -ne 'equal' -or $after.remote_commit -ne $snapshot.commit -or
        $after.last_pushed_commit -ne $snapshot.commit -or $afterDoctor.checks.save_root.manifest.root_hash -ne $beforeLiveRoot) {
        throw 'postconditions_changed'
    }
    $afterRestore = Get-RestoreOrThrow 'post' $snapshot.commit
    if ($afterRestore.root_hash -ne $beforeLiveRoot) { throw 'postconditions_changed' }

    $resultStatus = 'complete'; $resultCode = 'snapshot_pushed_live_preserved'
}
catch { if ($_.Exception.Message -in $allowedCodes) { $resultCode = $_.Exception.Message } }

Write-Output (Write-Sentinel $resultStatus $resultCode)
exit $(if ($resultStatus -eq 'complete') { 0 } else { 1 })
```

Report exactly the one JSON line printed by the block.
