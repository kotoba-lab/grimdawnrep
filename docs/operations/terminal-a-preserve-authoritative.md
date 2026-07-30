# Terminal A: preserve the authoritative live save

Use this runbook only for the enrolled Terminal A (`desktop-a`) after the
current live save on that terminal has been designated authoritative.  Grim
Dawn and DPYes must already be fully closed.  This runbook first makes a
verified, local-only archive of that live save; it does **not** recover,
launch, snapshot, restore, bootstrap, or alter the vault or lock.

The current remote snapshot may be older than Terminal A's live save.  Do not
use a sync command until this runbook has returned its success sentinel and a
separate instruction explicitly authorizes the next action.

## Boundaries

- Run the block in **Windows PowerShell 5.1**, not PowerShell 7.
- It pulls only the public source repository with `git pull --ff-only` so the
  installed source contains the `preserve` command.  It never accesses or
  prints the vault remote URL.
- It uses the existing virtual environment and `config.local.json`; it does
  not create, edit, delete, clean up, or repair configuration or archives.
- `preserve` itself is local-only and does not consult terminal state, the
  vault, the remote, or the sync lock.  The status calls around it are
  read-only checks.
- The block invokes `--json preserve` once as a dry run and once with
  `--apply`; it validates both JSON responses before continuing.
- Do not run launch, recover, snapshot, restore, bootstrap, push, retry,
  delete, cleanup, or process-kill commands.  Do not relay command output,
  paths, save names, hashes, counts, or archive IDs.

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
    'preconditions_changed', 'preserve_dry_run_failed', 'preserve_dry_run_parse_failed',
    'preserve_dry_run_shape_invalid', 'preserve_apply_failed',
    'preserve_apply_parse_failed', 'preserve_apply_shape_invalid',
    'post_status_failed', 'post_status_parse_failed', 'post_status_shape_invalid',
    'postconditions_changed', 'unexpected_failed'
)

function Write-Sentinel([string]$Status, [string]$Code) {
    [ordered]@{
        sentinel = 'TERMINAL_A_PRESERVE'
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

function Invoke-PreserveOrThrow([bool]$Apply) {
    $arguments = @('-m', 'grim_dawn_sync', '--config', $config, '--json', 'preserve')
    $stage = 'preserve_dry_run'
    if ($Apply) { $arguments += '--apply'; $stage = 'preserve_apply' }
    $output = @(& $python @arguments 2>$null)
    $exitCode = $LASTEXITCODE
    $raw = @($output) -join [Environment]::NewLine
    if ($exitCode -ne 0 -or -not $raw) { throw ($stage + '_failed') }
    try { $result = $raw | ConvertFrom-Json -ErrorAction Stop }
    catch { throw ($stage + '_parse_failed') }
    if ($result.schema_version -ne '1.0.0' -or $result.command -ne 'preserve') {
        throw ($stage + '_shape_invalid')
    }
    if (-not $Apply) {
        if ($result.dry_run -ne $true -or $result.verified -ne $false) {
            throw 'preserve_dry_run_shape_invalid'
        }
    } else {
        if ($result.dry_run -ne $false -or $result.verified -ne $true -or
            $result.archive_id -isnot [string] -or
            $result.archive_id -notmatch '^save-preserved-[0-9a-f]{16}-[0-9a-f]{32}$') {
            throw 'preserve_apply_shape_invalid'
        }
    }
    return $result
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

    # Recheck the user's closed-process claim and the expected retained-lock state.
    $before = Get-StatusOrThrow 'pre'
    $beforeLockPresent = ($before.active_lock -ne $null)
    $beforeRecoveryRequired = ($beforeLockPresent -or $before.recovery_phase -ne $null -or $before.readiness -eq 'recovery_required')
    $beforeRecoveryPhasePresent = ($before.recovery_phase -ne $null)
    if ($before.processes.status -ne 'clear' -or $before.processes.complete -ne $true -or
        -not $beforeLockPresent -or -not $beforeRecoveryRequired) { throw 'preconditions_changed' }

    [void](Invoke-PreserveOrThrow $false)
    [void](Invoke-PreserveOrThrow $true)

    $after = Get-StatusOrThrow 'post'
    $afterLockPresent = ($after.active_lock -ne $null)
    $afterRecoveryRequired = ($afterLockPresent -or $after.recovery_phase -ne $null -or $after.readiness -eq 'recovery_required')
    $afterRecoveryPhasePresent = ($after.recovery_phase -ne $null)
    if ($after.processes.status -ne 'clear' -or $after.processes.complete -ne $true -or
        $afterLockPresent -ne $beforeLockPresent -or
        $afterRecoveryRequired -ne $beforeRecoveryRequired -or
        $afterRecoveryPhasePresent -ne $beforeRecoveryPhasePresent) { throw 'postconditions_changed' }

    $resultStatus = 'complete'; $resultCode = 'live_archive_verified'
}
catch {
    if ($_.Exception.Message -in $allowedCodes) { $resultCode = $_.Exception.Message }
}

Write-Output (Write-Sentinel $resultStatus $resultCode)
exit $(if ($resultStatus -eq 'complete') { 0 } else { 1 })
```

Report exactly the one JSON line printed by the block.  On any blocked result,
stop and wait for a new instruction; do not attempt a second run or any repair.
