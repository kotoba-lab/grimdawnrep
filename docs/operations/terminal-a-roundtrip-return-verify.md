# Terminal A roundtrip: read-only return verification

Use this only when Terminal A's `A_RETURN` session already ended but its
post-flight check returned `post_doctor_shape_invalid`.  It does not start,
stop, or modify DPYes/Grim Dawn, live saves, Vault data, locks, or state.
DPYes may briefly remain after Grim Dawn exits; a remaining Grim Dawn process
is not permitted.

## Boundaries

- Use **Windows PowerShell 5.1**, not PowerShell 7.
- Apart from a fast-forward-only source update, this is read-only.
- Do not launch, snapshot, recover, bootstrap, enroll, restore with `--apply`,
  Git push/reset/clean, delete files, or terminate processes.
- Report only the resulting sanitized JSON line; do not include paths, URLs,
  save names, hashes, session IDs, or raw output.

## Copy-paste execution

```powershell
$source = Join-Path $env:USERPROFILE 'grimdawnrep'
$toolRoot = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool'
$python = Join-Path $toolRoot '.venv\Scripts\python.exe'
$config = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSync\config.local.json'
$machineId = 'desktop-a'
$allowedCodes = @('source_update_failed','environment_check_failed','config_check_failed','status_failed','status_parse_failed','status_shape_invalid','doctor_failed','doctor_parse_failed','doctor_shape_invalid','game_process_check_failed','game_still_running','restore_failed','restore_parse_failed','restore_shape_invalid','log_shape_invalid','postconditions_changed','unexpected_failed')
function Write-Sentinel([string]$Status,[string]$Code) { [ordered]@{sentinel='TERMINAL_A_ROUNDTRIP';status=$Status;leg='A_RETURN_VERIFY';code=$Code;machine_id=$machineId}|ConvertTo-Json -Compress }
function Get-JsonOrThrow([string]$Name,[string[]]$Arguments) {
    $output=@(& $python -m grim_dawn_sync --config $config --json @Arguments 2>$null); $exitCode=$LASTEXITCODE; $raw=@($output)-join [Environment]::NewLine
    if($exitCode -ne 0 -or -not $raw){throw($Name+'_failed')}; try{return($raw|ConvertFrom-Json -ErrorAction Stop)}catch{throw($Name+'_parse_failed')}
}
function Assert-GrimDawnNotRunning { try{$game=@(Get-Process -Name 'Grim Dawn' -ErrorAction SilentlyContinue)}catch{throw 'game_process_check_failed'}; if($game.Count -ne 0){throw 'game_still_running'} }
function Assert-LastLaunchComplete([string]$Commit) {
    $logPath=Join-Path ([System.IO.Path]::GetDirectoryName($config)) 'logs\launch.jsonl'
    if(-not(Test-Path -LiteralPath $logPath -PathType Leaf)){throw 'log_shape_invalid'}
    try{$rows=@(Get-Content -LiteralPath $logPath -Encoding utf8 -ErrorAction Stop|ForEach-Object{$_|ConvertFrom-Json -ErrorAction Stop})}catch{throw 'log_shape_invalid'}
    $latest=@($rows|Where-Object{$_.machine_id -eq $machineId}|Select-Object -Last 1)
    if($latest.Count -ne 1 -or $latest[0].schema_version -ne '1.0.0' -or $latest[0].state -ne 'COMPLETE' -or $latest[0].event -ne 'entered' -or $latest[0].PSObject.Properties.Name -contains 'code' -or $latest[0].safe_oid -ne $Commit){throw 'log_shape_invalid'}
}
$resultStatus='blocked';$resultCode='unexpected_failed'
try {
    if(-not(Test-Path -LiteralPath $source -PathType Container)){throw 'source_update_failed'}; & git -C $source pull --ff-only *> $null; if($LASTEXITCODE -ne 0){throw 'source_update_failed'}
    if(-not(Test-Path -LiteralPath $python -PathType Leaf)-or -not(Test-Path -LiteralPath $config -PathType Leaf)){throw 'environment_check_failed'}
    try{$existingConfig=Get-Content -LiteralPath $config -Raw -Encoding utf8|ConvertFrom-Json -ErrorAction Stop}catch{throw 'config_check_failed'};if($existingConfig.machine_id -ne $machineId){throw 'config_check_failed'}
    $status=Get-JsonOrThrow 'status' @('status');if($status.schema_version -ne '1.0.0' -or $status.command -ne 'status'){throw 'status_shape_invalid'}
    if($status.active_lock -ne $null -or $status.recovery_phase -ne $null -or $status.readiness -ne 'ready' -or $status.vault_relation -ne 'equal' -or $status.remote_commit -notmatch '^[0-9a-f]{40}(?:[0-9a-f]{24})?$' -or $status.last_pushed_commit -ne $status.remote_commit -or $status.processes.complete -ne $true){throw 'postconditions_changed'};$commit=[string]$status.remote_commit
    $doctor=Get-JsonOrThrow 'doctor' @('doctor');if($doctor.schema_version -ne '1.0.0' -or $doctor.command -ne 'doctor' -or $doctor.read_only -ne $true -or $doctor.machine_id -ne $machineId -or $doctor.checks.processes.complete -ne $true -or $doctor.checks.processes.status -notin @('clear','running') -or $doctor.checks.save_root.manifest.root_hash -notmatch '^[0-9a-f]{64}$'){throw 'doctor_shape_invalid'};Assert-GrimDawnNotRunning
    $restore=Get-JsonOrThrow 'restore' @('restore','--commit',$commit);if($restore.schema_version -ne '1.0.0' -or $restore.command -ne 'restore' -or $restore.dry_run -ne $true -or $restore.commit -ne $commit -or $restore.root_hash -ne $doctor.checks.save_root.manifest.root_hash){throw 'restore_shape_invalid'};Assert-LastLaunchComplete $commit
    $resultStatus='complete';$resultCode='roundtrip_verified'
}catch{if($_.Exception.Message -in $allowedCodes){$resultCode=$_.Exception.Message}}
Write-Output(Write-Sentinel $resultStatus $resultCode);exit $(if($resultStatus -eq 'complete'){0}else{1})
```

Expected result:

```json
{"sentinel":"TERMINAL_A_ROUNDTRIP","status":"complete","leg":"A_RETURN_VERIFY","code":"roundtrip_verified","machine_id":"desktop-a"}
```
