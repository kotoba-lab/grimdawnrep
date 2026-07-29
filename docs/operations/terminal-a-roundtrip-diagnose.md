# Terminal A roundtrip: read-only A1 diagnosis

Use this runbook only after A1 reports `launch_failed`.  It performs a
read-only diagnosis of the already enrolled Terminal A (`desktop-a`).  It
does not pull public source first: even a fast-forward pull changes local
state and is unnecessary for this diagnosis.

## Boundaries

- Run the block in **Windows PowerShell 5.1**, not PowerShell 7.
- Do not launch again, recover, repair, bootstrap, snapshot, restore, push,
  reset, rebase, force, edit configuration, or stop any process.
- The existing virtual environment and local configuration are required.  The
  block runs only `--json status` and `--json doctor`, and reads the
  local launch audit log if it already exists.
- Do not print or relay the private remote URL, local paths, save names,
  hashes, session identifiers, or raw command output.  The only report is the
  final sentinel line below.

## Copy-paste execution

The block suppresses command output, validates the existing configuration's
machine ID, then reduces observations to allow-listed enums and booleans.  A
missing or malformed audit log remains a diagnosis result; it does not cause
any repair action.

```powershell
$toolRoot = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool'
$python = Join-Path $toolRoot '.venv\Scripts\python.exe'
$config = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSync\config.local.json'
$machineId = 'desktop-a'

function Write-Diagnosis([string]$Status, [string]$Code, [bool]$RecoveryRequired, [bool]$LockPresent,
    [string]$Processes, [string]$Readiness, [string]$VaultRelation,
    [string]$LogErrorCode, [string]$LogLastState, [string]$LogNextCommand) {
    [ordered]@{
        sentinel = 'TERMINAL_A_DIAGNOSIS'
        status = $Status
        code = $Code
        machine_id = $machineId
        recovery_required = $RecoveryRequired
        lock_present = $LockPresent
        processes = $Processes
        readiness = $Readiness
        vault_relation = $VaultRelation
        log_error_code = $LogErrorCode
        log_last_state = $LogLastState
        log_next_command = $LogNextCommand
    } | ConvertTo-Json -Compress
}

function Get-StatusOrThrow {
    $output = @(& $python -m grim_dawn_sync --config $config --json status 2>$null)
    $exitCode = $LASTEXITCODE
    $raw = @($output) -join [Environment]::NewLine
    if ($exitCode -ne 0 -or -not $raw) { throw 'status_command_failed' }
    try { $status = $raw | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'status_parse_failed' }
    if ($status.schema_version -ne '1.0.0' -or $status.command -ne 'status') { throw 'status_shape_invalid' }
    return $status
}

function Get-DoctorOptional {
    try {
        $output = @(& $python -m grim_dawn_sync --config $config --json doctor 2>$null)
        $exitCode = $LASTEXITCODE
        $raw = @($output) -join [Environment]::NewLine
        if ($exitCode -ne 0 -or -not $raw) { return $false }
        $doctor = $raw | ConvertFrom-Json -ErrorAction Stop
        return ($doctor.schema_version -eq '1.0.0' -and $doctor.command -eq 'doctor' -and $doctor.read_only -and $doctor.machine_id -eq $machineId)
    }
    catch { return $false }
}

function Get-ProcessClass($Status) {
    if ($null -eq $Status -or $Status.complete -ne $true) { return 'unknown_incomplete' }
    if ($Status.status -eq 'clear') { return 'clear_complete' }
    if ($Status.status -eq 'running') { return 'running_complete' }
    return 'invalid'
}

function Get-ReadinessClass($Value) {
    if ($Value -in @('ready', 'blocked', 'recovery_required')) { return [string]$Value }
    return 'unknown'
}

function Get-VaultRelationClass($Value) {
    if ($Value -in @('equal', 'remote_changed_or_unknown', 'unborn', 'remote_missing', 'behind')) { return [string]$Value }
    return 'unknown'
}

function Get-LaunchFailure([string]$LogPath) {
    $result = [ordered]@{ code = 'none'; last_state = 'none'; next_command = 'none' }
    if (-not (Test-Path -LiteralPath $LogPath -PathType Leaf)) { return [pscustomobject]$result }
    try { $lines = @(Get-Content -LiteralPath $LogPath -Encoding utf8 -ErrorAction Stop) }
    catch { return [pscustomobject]$result }
    for ($index = $lines.Count - 1; $index -ge 0; $index--) {
        try { $row = $lines[$index] | ConvertFrom-Json -ErrorAction Stop }
        catch { continue }
        if ($row.schema_version -ne '1.0.0' -or $row.event -ne 'failed') { continue }
        if ($row.code -is [string] -and $row.code -match '^[A-Za-z0-9_.-]{1,128}$') { $result.code = $row.code }
        if ($row.last_successful_state -in @('PREFLIGHT','FETCH_REMOTE','RECONCILE','ACQUIRE_LOCK','ARCHIVE_BEFORE_RESTORE','APPLY_REMOTE_SAVE','START_DPYES','WAIT_GAME_START','WAIT_GAME_EXIT','WAIT_SAVE_STABLE','VALIDATE_SAVE','ARCHIVE_AFTER_GAME','UPDATE_VAULT','COMMIT','PUSH','RELEASE_LOCK','COMPLETE')) { $result.last_state = $row.last_successful_state }
        if ($row.next_command -in @('grim-dawn-sync recover', 'grim-dawn-sync status')) { $result.next_command = $row.next_command }
        return [pscustomobject]$result
    }
    return [pscustomobject]$result
}

$processes = 'unknown_incomplete'; $readiness = 'unknown'; $vaultRelation = 'unknown'
$recoveryRequired = $false; $lockPresent = $false
$failure = [pscustomobject]@{ code = 'none'; last_state = 'none'; next_command = 'none' }
$statusOut = 'blocked'; $code = 'environment_check_failed'

try {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $config -PathType Leaf)) { throw 'environment_check_failed' }
    try { $existingConfig = Get-Content -LiteralPath $config -Raw -Encoding utf8 | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'config_check_failed' }
    if ($existingConfig.machine_id -ne $machineId) { throw 'config_check_failed' }

    $status = Get-StatusOrThrow

    $processes = Get-ProcessClass $status.processes
    $readiness = Get-ReadinessClass $status.readiness
    $vaultRelation = Get-VaultRelationClass $status.vault_relation
    $lockPresent = ($status.active_lock -ne $null)
    $recoveryRequired = ($lockPresent -or $status.recovery_phase -ne $null -or $readiness -eq 'recovery_required')
    $statusOut = 'diagnosed'; $code = 'diagnosis_complete'

    # Optional observations must never downgrade an otherwise valid status diagnosis.
    [void](Get-DoctorOptional)
    try {
        $logPath = Join-Path ([System.IO.Path]::GetDirectoryName($config)) 'logs\launch.jsonl'
        $failure = Get-LaunchFailure $logPath
    }
    catch { $failure = [pscustomobject]@{ code = 'none'; last_state = 'none'; next_command = 'none' } }
}
catch {
    if ($_.Exception.Message -in @('environment_check_failed','config_check_failed','status_command_failed','status_parse_failed','status_shape_invalid')) { $code = $_.Exception.Message }
}

Write-Output (Write-Diagnosis $statusOut $code $recoveryRequired $lockPresent $processes $readiness $vaultRelation $failure.code $failure.last_state $failure.next_command)
exit $(if ($statusOut -eq 'diagnosed') { 0 } else { 1 })
```

Report exactly the one JSON line printed by the block.  Do not add raw output
or a proposed recovery command.
