# Install the Save Sync desktop shortcut

Run this separately on Terminal A and Terminal B, only after each terminal's
enrollment. It read-only inspects an existing `Grim Dawn (DPYes + Save
Sync).lnk`, then atomically creates `Grim Dawn (DPYes + Save Selection).lnk`
once. It never replaces, deletes, or edits either old shortcut or the legacy
`Grim Dawn (DPYes).lnk` shortcut.

After installation, use the normal selection window for GUI bookmarks or the
[live bookmark runbook](save-sync-live-bookmark.md) for headless CLI and
recovery steps.

## Copy-paste execution (Windows PowerShell 5.1)

```powershell
$source=Join-Path $env:USERPROFILE 'grimdawnrep'; $config=Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSync\config.local.json'; $venvPython=Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool\.venv\Scripts\python.exe'; $desktop=Join-Path $env:USERPROFILE 'Desktop'; $machineId=''
function Out-Sentinel([string]$s,[string]$c){[ordered]@{sentinel='SAVE_SYNC_SHORTCUT';status=$s;code=$c;machine_id=$machineId}|ConvertTo-Json -Compress}
function Fingerprint([string]$p){if(-not(Test-Path -LiteralPath $p -PathType Leaf)){return $null};$i=Get-Item -LiteralPath $p -Force;return ((Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash+':'+$i.Length+':'+$i.LastWriteTimeUtc.Ticks)}
try {
  $checkout=(& git -C $source rev-parse --show-toplevel 2>$null).Trim();if($LASTEXITCODE -ne 0 -or [IO.Path]::GetFullPath($checkout) -ne [IO.Path]::GetFullPath($source)){throw 'source_checkout_mismatch'}
  & git -C $source pull --ff-only *> $null;if($LASTEXITCODE -ne 0){throw 'source_pull_failed'}
  if(-not(Test-Path -LiteralPath $config -PathType Leaf)-or -not(Test-Path -LiteralPath (Join-Path $source 'src') -PathType Container)){throw 'source_or_config_missing'}
  $python=$null;$pythonVersion=$null;$pythonCandidates=@();if(Test-Path -LiteralPath $venvPython -PathType Leaf){$pythonCandidates+=,$venvPython};$pythonCandidates+=@(Get-Command python -CommandType Application -ErrorAction SilentlyContinue|ForEach-Object{[string]$_.Source})
  foreach($candidate in $pythonCandidates){if(-not(Test-Path -LiteralPath $candidate -PathType Leaf)){continue};$candidatePath=[IO.Path]::GetFullPath([string]$candidate);$candidateVersion=(& $candidatePath -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null).Trim();if($LASTEXITCODE -eq 0 -and [version]$candidateVersion -ge [version]'3.11'){$python=$candidatePath;$pythonVersion=$candidateVersion;break}}
  if(-not $python){throw 'python_version_unsupported'}
  # Only children of this runbook receive the checkout import root; the
  # generated shortcut embeds it in its fixed Python payload instead.
  $env:PYTHONPATH=Join-Path $source 'src'
  $cfg=Get-Content -LiteralPath $config -Raw|ConvertFrom-Json;$machineId=[string]$cfg.machine_id;if($machineId -notmatch '^[A-Za-z0-9_.-]{1,128}$'){throw 'configured_machine_invalid'}
  if(Get-Process -Name 'Grim Dawn','DPYes' -ErrorAction SilentlyContinue){throw 'game_or_dpyes_running'}
  $legacy=Join-Path $desktop 'Grim Dawn (DPYes).lnk';$legacyBefore=Fingerprint $legacy;$previous=Join-Path $desktop 'Grim Dawn (DPYes + Save Sync).lnk';$previousBefore=Fingerprint $previous
  & $python -m grim_dawn_sync --config $config --json migrate-shortcut *> $null;if($LASTEXITCODE -ne 0){throw 'shortcut_dry_run_failed'}
  & $python -m grim_dawn_sync --config $config --json migrate-shortcut --apply *> $null;if($LASTEXITCODE -ne 0){throw 'shortcut_apply_failed'}
  $created=Join-Path $desktop 'Grim Dawn (DPYes + Save Selection).lnk';if(-not(Test-Path -LiteralPath $created -PathType Leaf)){throw 'shortcut_missing'}
  $shell=New-Object -ComObject WScript.Shell;$link=$shell.CreateShortcut($created);$target=[IO.Path]::GetFullPath($python);$working=[IO.Path]::GetFullPath((Join-Path $source 'src'))
  if($link.TargetPath -ne $target -or [string]::IsNullOrWhiteSpace($link.Arguments) -or $link.WorkingDirectory -ne $working){throw 'shortcut_shape_invalid'}
  if($link.Arguments -notmatch '^-c "' -or $link.Arguments -notmatch 'grim_dawn_sync\.cli' -or $link.Arguments -notmatch "'launch'" -or $link.Arguments -notmatch "'--config'"){throw 'shortcut_arguments_invalid'}
  if((Fingerprint $legacy) -ne $legacyBefore -or (Fingerprint $previous) -ne $previousBefore){throw 'legacy_shortcut_changed'}
  Write-Output (Out-Sentinel 'complete' 'shortcut_installed_verified');exit 0
}catch{$code=if($_.Exception.Message -match '^[a-z0-9_]+$'){$_.Exception.Message}else{'shortcut_install_failed'};Write-Output (Out-Sentinel 'blocked' $code);exit 1}
```

It verifies the exact target, fixed arguments, and working directory through a
read-only COM inspection. An old checkout is reported as stale, while the old
file's hash, length, and timestamp remain unchanged. The new shortcut is
published create-only through a temporary `.lnk` and atomic move. Expected success:

```json
{"sentinel":"SAVE_SYNC_SHORTCUT","status":"complete","code":"shortcut_installed_verified","machine_id":"desktop-a"}
```
