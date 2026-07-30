# Terminal A roundtrip: return from Terminal B

Use this final acceptance leg only after Terminal B has completed its normal
DPYes/Grim Dawn session and published its new commit.  Terminal A must still
be at its older, unchanged baseline.  The protected `launch` workflow is the
only operation which may reconcile the newer remote save, start the game, and
publish the normal post-exit result.

## Required user coordination

Tell the user that the block opens DPYes/Grim Dawn once.  When it opens, check
that the expected current save is present, then exit both applications
normally.  No additional play or deliberate save change is needed.  Do not
start another game instance.  The command waits for the normal exit and save
stabilization before it returns.

## Boundaries

- Use **Windows PowerShell 5.1**, not PowerShell 7.
- Run the block exactly once.  A blocked result means stop for a new
  instruction; never retry `launch`.
- It checks the existing source, virtual environment, and configuration only.
  The source update is fast-forward-only; the existing `.pth` virtual
  environment uses the updated source directly.
- Do not run recover, snapshot directly, restore with `--apply`, bootstrap,
  enroll, manual Git push/reset/clean/delete, or process termination.
- It prints exactly one sanitized JSON line.  Do not relay paths, URLs, save
  names, commit IDs, roots, session IDs, or raw command output.

## Copy-paste execution

```powershell
$source = Join-Path $env:USERPROFILE 'grimdawnrep'
$toolRoot = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool'
$python = Join-Path $toolRoot '.venv\Scripts\python.exe'
$config = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSync\config.local.json'
$machineId = 'desktop-a'
$allowedCodes = @(
    'source_update_failed', 'environment_check_failed', 'config_check_failed',
    'pre_status_failed', 'pre_status_parse_failed', 'pre_status_shape_invalid',
    'pre_doctor_failed', 'pre_doctor_parse_failed', 'pre_doctor_shape_invalid',
    'pre_restore_failed', 'pre_restore_parse_failed', 'pre_restore_shape_invalid',
    'preconditions_changed', 'launch_failed', 'launch_parse_failed',
    'launch_shape_invalid', 'post_status_failed', 'post_status_parse_failed',
    'post_status_shape_invalid', 'post_doctor_failed', 'post_doctor_parse_failed',
    'post_doctor_shape_invalid', 'post_game_process_check_failed', 'post_game_still_running', 'post_restore_failed', 'post_restore_parse_failed',
    'post_restore_shape_invalid', 'postconditions_changed', 'unexpected_failed'
)

function Write-Sentinel([string]$Status, [string]$Code) {
    [ordered]@{ sentinel = 'TERMINAL_A_ROUNDTRIP'; status = $Status; leg = 'A_RETURN'; code = $Code; machine_id = $machineId } |
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

function Get-DoctorOrThrow([string]$Prefix, [bool]$AllowDPYesOnly) {
    $output = @(& $python -m grim_dawn_sync --config $config --json doctor 2>$null)
    $exitCode = $LASTEXITCODE; $raw = @($output) -join [Environment]::NewLine
    if ($exitCode -ne 0 -or -not $raw) { throw ($Prefix + '_doctor_failed') }
    try { $value = $raw | ConvertFrom-Json -ErrorAction Stop } catch { throw ($Prefix + '_doctor_parse_failed') }
    if ($value.schema_version -ne '1.0.0' -or $value.command -ne 'doctor' -or $value.read_only -ne $true -or
        $value.machine_id -ne $machineId -or $value.checks.processes.complete -ne $true -or
        $value.checks.save_root.manifest.root_hash -notmatch '^[0-9a-f]{64}$') {
        throw ($Prefix + '_doctor_shape_invalid')
    }
    if ($AllowDPYesOnly) {
        if ($value.checks.processes.status -notin @('clear', 'running')) { throw ($Prefix + '_doctor_shape_invalid') }
    }
    elseif ($value.checks.processes.status -ne 'clear') { throw ($Prefix + '_doctor_shape_invalid') }
    return $value
}

function Assert-GrimDawnNotRunning([string]$Prefix) {
    try { $game = @(Get-Process -Name 'Grim Dawn' -ErrorAction SilentlyContinue) }
    catch { throw ($Prefix + '_game_process_check_failed') }
    if ($game.Count -ne 0) { throw ($Prefix + '_game_still_running') }
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

function Invoke-LaunchOnceOrThrow {
    $output = @(& $python -m grim_dawn_sync --config $config --json launch 2>$null)
    $exitCode = $LASTEXITCODE; $raw = @($output) -join [Environment]::NewLine
    if ($exitCode -ne 0 -or -not $raw) { throw 'launch_failed' }
    try { $value = $raw | ConvertFrom-Json -ErrorAction Stop } catch { throw 'launch_parse_failed' }
    if ($value.schema_version -ne '1.0.0' -or $value.command -ne 'launch' -or $value.result.state -ne 'COMPLETE' -or
        $value.result.commit -notmatch '^[0-9a-f]{40}(?:[0-9a-f]{24})?$') { throw 'launch_shape_invalid' }
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
    $beforeDoctor = Get-DoctorOrThrow 'pre' $false
    if ($before.processes.status -ne 'clear' -or $before.processes.complete -ne $true -or
        $before.active_lock -ne $null -or $before.recovery_phase -ne $null -or
        $before.readiness -ne 'blocked' -or $before.vault_relation -ne 'remote_changed_or_unknown' -or
        $before.remote_commit -notmatch '^[0-9a-f]{40}(?:[0-9a-f]{24})?$' -or
        $before.last_pushed_commit -notmatch '^[0-9a-f]{40}(?:[0-9a-f]{24})?$' -or
        $before.remote_commit -eq $before.last_pushed_commit) { throw 'preconditions_changed' }
    $beforeRemote = [string]$before.remote_commit
    $oldBaseline = [string]$before.last_pushed_commit
    $beforeLiveRoot = [string]$beforeDoctor.checks.save_root.manifest.root_hash
    $oldRestore = Get-RestoreOrThrow 'pre' $oldBaseline
    if ($oldRestore.root_hash -ne $beforeLiveRoot) { throw 'preconditions_changed' }

    $launch = Invoke-LaunchOnceOrThrow
    if ($launch.result.commit -eq $beforeRemote) { throw 'launch_shape_invalid' }

    $after = Get-StatusOrThrow 'post'
    # DPYes can outlive the game's process briefly.  Its post-exit presence is
    # permitted only after a direct check proves Grim Dawn itself is absent.
    $afterDoctor = Get-DoctorOrThrow 'post' $true
    Assert-GrimDawnNotRunning 'post'
    if ($after.processes.status -notin @('clear', 'running') -or $after.processes.complete -ne $true -or
        $after.active_lock -ne $null -or $after.recovery_phase -ne $null -or $after.readiness -ne 'ready' -or
        $after.vault_relation -ne 'equal' -or $after.remote_commit -ne $launch.result.commit -or
        $after.last_pushed_commit -ne $launch.result.commit -or $after.remote_commit -eq $beforeRemote) {
        throw 'postconditions_changed'
    }
    $afterLiveRoot = [string]$afterDoctor.checks.save_root.manifest.root_hash
    $newRestore = Get-RestoreOrThrow 'post' $launch.result.commit
    if ($newRestore.root_hash -ne $afterLiveRoot) { throw 'postconditions_changed' }

    $resultStatus = 'complete'; $resultCode = 'roundtrip_complete'
}
catch { if ($_.Exception.Message -in $allowedCodes) { $resultCode = $_.Exception.Message } }

Write-Output (Write-Sentinel $resultStatus $resultCode)
exit $(if ($resultStatus -eq 'complete') { 0 } else { 1 })
```

Report exactly the one JSON line printed by the block.  A successful result is:

```json
{"sentinel":"TERMINAL_A_ROUNDTRIP","status":"complete","leg":"A_RETURN","code":"roundtrip_complete","machine_id":"desktop-a"}
```
