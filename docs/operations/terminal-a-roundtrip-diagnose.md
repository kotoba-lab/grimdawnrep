# Terminal A roundtrip: read-only A1 remote diff summary

Use this runbook only after A1 reports `remote_changed_or_unknown`. It
summarizes the already enrolled Terminal A (`desktop-a`) without changing a
save, state, configuration, source worktree, or Vault ref.

## Operator-mediated public remote request

The request at `ops/handoff/terminal-a-diagnostic-request.v1.json` is inert
coordination data, not a command, script, configuration, or authorization to
run fetched source. An operator may retrieve it only from the canonical
public `origin/master`; no other remote or branch is accepted. Run this block
in **Windows PowerShell 5.1**. It emits nothing on success. On any failure it
emits one allow-listed blocked sentinel identifying only the failed stage and
stops without disclosing the request,
remote URL, object IDs, local paths, or Git output.

```powershell
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$source = Join-Path $env:USERPROFILE 'grimdawnrep'
$toolRoot = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool'
$python = Join-Path $toolRoot '.venv\Scripts\python.exe'
$config = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSync\config.local.json'
$machineId = 'desktop-a'
$publicUrl = 'https://github.com/kotoba-lab/grimdawnrep.git'
$requestObjectPath = 'ops/handoff/terminal-a-diagnostic-request.v1.json'
$oidPattern = '^[0-9a-f]{40}(?:[0-9a-f]{24})?$'
$stage = 'origin_identity'
$stageCodes = @{
    origin_identity = 'origin_identity_invalid'
    source_branch = 'source_branch_invalid'
    source_clean = 'source_clean_invalid'
    fingerprint = 'fingerprint_invalid'
    fetch = 'fetch_failed'
    oid = 'oid_invalid'
    ancestor = 'ancestor_invalid'
    blob = 'blob_invalid'
    schema = 'schema_invalid'
    time = 'time_invalid'
    post_invariant = 'post_invariant_invalid'
}

function Write-RequestBlocked {
    $safeStages = @('origin_identity','source_branch','source_clean','fingerprint','fetch','oid',
        'ancestor','blob','schema','time','post_invariant')
    $safeStage = if ($stage -in $safeStages) { $stage } else { 'origin_identity' }
    [ordered]@{
        sentinel = 'TERMINAL_A_REMOTE_CLASSIFICATION'
        status = 'blocked'
        leg = 'A1'
        machine_id = $machineId
        stage = $safeStage
        code = [string]$stageCodes[$safeStage]
    } | ConvertTo-Json -Compress
}

function Invoke-GitLines([string[]]$GitArgs) {
    $lines = @(& git -C $source @GitArgs 2>$null)
    if ($LASTEXITCODE -ne 0) { throw 'invalid' }
    return @($lines)
}

function Get-OneGitLine([string[]]$GitArgs) {
    $lines = @(Invoke-GitLines $GitArgs)
    if ($lines.Count -ne 1) { throw 'invalid' }
    return ([string]$lines[0]).Trim()
}

function Invoke-GitQuiet([string[]]$GitArgs) {
    $priorErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & git -C $source @GitArgs 1>$null 2>$null
        $gitExitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $priorErrorAction }
    if ($gitExitCode -ne 0) { throw 'invalid' }
}

function Assert-ExactArray($Actual, [string[]]$Expected) {
    $values = @($Actual)
    if ($values.Count -ne $Expected.Count) { throw 'invalid' }
    for ($index = 0; $index -lt $Expected.Count; $index++) {
        if (-not ($values[$index] -is [string]) -or $values[$index] -cne $Expected[$index]) { throw 'invalid' }
    }
}

function Get-StrictUtc([string]$Value) {
    if ($Value -cnotmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$') { throw 'invalid' }
    $parsed = [DateTimeOffset]::MinValue
    $style = [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal
    if (-not [DateTimeOffset]::TryParseExact($Value, 'yyyy-MM-ddTHH:mm:ssZ',
        [Globalization.CultureInfo]::InvariantCulture, $style, [ref]$parsed)) { throw 'invalid' }
    return $parsed
}

function Get-RawStrictUtc([string]$Raw, [string]$Key) {
    $escapedKey = [Regex]::Escape($Key)
    $keyMatches = [Regex]::Matches($Raw, '(?<!\\)"' + $escapedKey + '"\s*:')
    $valueMatches = [Regex]::Matches($Raw, '(?<!\\)"' + $escapedKey + '"\s*:\s*"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)"')
    if ($keyMatches.Count -ne 1 -or $valueMatches.Count -ne 1) { throw 'invalid' }
    return Get-StrictUtc $valueMatches[0].Groups[1].Value
}

function Get-FileSha256([string]$Path) {
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '') }
    finally { $sha.Dispose(); $stream.Dispose() }
}

try {
    $stage = 'origin_identity'
    if (-not (Test-Path -LiteralPath $source -PathType Container)) { throw 'invalid' }
    $fetchUrls = @(Invoke-GitLines @('remote','get-url','--all','origin'))
    $pushUrls = @(Invoke-GitLines @('remote','get-url','--push','--all','origin'))
    if ($fetchUrls.Count -ne 1 -or $pushUrls.Count -ne 1 -or
        ([string]$fetchUrls[0]).Trim() -cne $publicUrl -or
        ([string]$pushUrls[0]).Trim() -cne $publicUrl) { throw 'invalid' }

    $stage = 'source_branch'
    $beforeBranch = Get-OneGitLine @('symbolic-ref','--quiet','HEAD')
    $beforeHead = Get-OneGitLine @('rev-parse','HEAD')
    if ($beforeBranch -cne 'refs/heads/master') { throw 'invalid' }

    $stage = 'source_clean'
    $beforeStatus = @(Invoke-GitLines @('status','--porcelain=v1','--untracked-files=all'))
    if ($beforeStatus.Count -ne 0) { throw 'invalid' }

    $stage = 'fingerprint'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
        -not (Test-Path -LiteralPath $config -PathType Leaf)) { throw 'invalid' }
    $beforePython = Get-FileSha256 $python
    $beforeConfig = Get-FileSha256 $config

    $stage = 'fetch'
    Invoke-GitQuiet @('fetch','--no-tags','origin','master:refs/remotes/origin/master')

    $stage = 'oid'
    $originMaster = Get-OneGitLine @('rev-parse','origin/master')
    $fetchHead = Get-OneGitLine @('rev-parse','FETCH_HEAD')
    if ($beforeHead -cnotmatch $oidPattern -or $originMaster -cnotmatch $oidPattern -or
        $fetchHead -cnotmatch $oidPattern -or $originMaster -cne $fetchHead) { throw 'invalid' }

    $stage = 'ancestor'
    Invoke-GitQuiet @('merge-base','--is-ancestor',$beforeHead,$fetchHead)
    $requestCommit = $fetchHead

    $stage = 'blob'
    $requestLines = @(& git -C $source show "$requestCommit`:$requestObjectPath" 2>$null)
    if ($LASTEXITCODE -ne 0 -or $requestLines.Count -eq 0) { throw 'invalid' }
    $requestRaw = @($requestLines) -join "`n"
    $requestBytes = [Text.Encoding]::UTF8.GetByteCount($requestRaw + "`n")
    if ($requestBytes -gt 4096 -or $requestRaw.Length -eq 0 -or
        $requestRaw[0] -eq [char]0xFEFF -or $requestRaw.Contains([char]0xFFFD) -or
        $requestRaw -match '[^\x09\x0A\x0D\x20-\x7E]') { throw 'invalid' }
    $stage = 'schema'
    try { $request = $requestRaw | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'invalid' }

    $expectedKeys = @('action','checks','constraints','expires_at','issued_at','kind','leg','not_before',
        'observed_code','request_id','response_sentinel','schema_version','sequence','target_machine_id')
    $actualKeys = @($request.PSObject.Properties.Name | Sort-Object)
    Assert-ExactArray $actualKeys $expectedKeys
    if (-not ($request.schema_version -is [string]) -or $request.schema_version -cne '1.0.0' -or
        -not ($request.kind -is [string]) -or
        $request.kind -cne 'grim_dawn_terminal_diagnostic_request' -or
        -not ($request.sequence -is [int]) -or $request.sequence -ne 7 -or
        -not ($request.target_machine_id -is [string]) -or $request.target_machine_id -cne $machineId -or
        -not ($request.leg -is [string]) -or -not ($request.observed_code -is [string]) -or
        -not ($request.action -is [string]) -or -not ($request.response_sentinel -is [string]) -or
        -not ($request.request_id -is [string]) -or
        $request.leg -cne 'A1' -or $request.observed_code -cne 'remote_changed_or_unknown' -or
        $request.action -cne 'summarize_remote_diff_readonly' -or
        $request.response_sentinel -cne 'TERMINAL_A_REMOTE_DIFF_SUMMARY') { throw 'invalid' }
    Assert-ExactArray $request.checks @('status_baseline','doctor','validated_remote_and_baseline_manifest','remote_diff_summary')
    Assert-ExactArray $request.constraints @('no_game_launch','no_lock','no_recover',
        'no_restore_snapshot_bookmark_promote','no_commit_push_merge_rebase_reset_checkout',
        'no_state_config_save_remote_ref_write','vault_readonly_status_rev_parse_ls_remote_fetch_merge_base_manifest_validate_diff',
        'no_fetched_code_execution')
    $requestGuid = [Guid]::Empty
    if (-not [Guid]::TryParse([string]$request.request_id, [ref]$requestGuid) -or
        $requestGuid.ToString() -cne [string]$request.request_id) { throw 'invalid' }

    $stage = 'time'
    $issued = Get-RawStrictUtc $requestRaw 'issued_at'
    $notBefore = Get-RawStrictUtc $requestRaw 'not_before'
    $expires = Get-RawStrictUtc $requestRaw 'expires_at'
    $now = [DateTimeOffset]::UtcNow
    if ($issued -gt $notBefore -or $notBefore -ge $expires -or
        ($expires - $notBefore).TotalSeconds -gt 4500 -or $now -lt $notBefore -or $now -ge $expires) { throw 'invalid' }

    $stage = 'post_invariant'
    $afterBranch = Get-OneGitLine @('symbolic-ref','--quiet','HEAD')
    $afterHead = Get-OneGitLine @('rev-parse','HEAD')
    $afterStatus = @(Invoke-GitLines @('status','--porcelain=v1','--untracked-files=all'))
    $afterPython = Get-FileSha256 $python
    $afterConfig = Get-FileSha256 $config
    if ($afterBranch -cne $beforeBranch -or $afterHead -cne $beforeHead -or
        $afterStatus.Count -ne 0 -or $afterPython -cne $beforePython -or $afterConfig -cne $beforeConfig) { throw 'invalid' }
    exit 0
}
catch {
    Write-Output (Write-RequestBlocked)
    exit 1
}
```

Continue only when the block exits zero and prints no output. Then run only
the already deployed local diagnosis block below. Do not execute or import
fetched code and do not replace the local block with code from the fetched
commit. If the request block prints its sentinel, relay exactly that one line
and stop. The original A1 contract remains in force: no pull, checkout, reset,
merge, rebase, push, launch retry, recovery, or save mutation.

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

Run this deployed, local classification block only after the request block
exits zero. It prints exactly one allow-listed sentinel. It never prints a
remote URL, object ID, manifest root, path, save name, or native stderr. If
the remote changes while it is examined, it reports `blocked/unknown`.

```powershell
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$toolRoot = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool'
$python = Join-Path $toolRoot '.venv\Scripts\python.exe'
$config = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSync\config.local.json'
$machineId = 'desktop-a'
$source = Join-Path $env:USERPROFILE 'grimdawnrep'
$shortcut = Join-Path (Join-Path $env:USERPROFILE 'Desktop') 'Grim Dawn (DPYes + Save Selection).lnk'
$oidPattern = '^[0-9a-f]{40}(?:[0-9a-f]{24})?$'

function Write-Classification([string]$Status, [string]$Classification, [string]$Content, [string]$Code) {
    if ($Status -notin @('complete','blocked')) { $Status = 'blocked' }
    if ($Classification -notin @('equal','remote_ahead','remote_behind','diverged','unknown')) { $Classification = 'unknown' }
    if ($Content -notin @('same','different','unknown')) { $Content = 'unknown' }
    if ($Code -notin @('safe_remote_ahead','remote_content_differs','not_target_relation','observation_changed')) { $Code = 'observation_changed' }
    [ordered]@{ sentinel = 'TERMINAL_A_REMOTE_CLASSIFICATION'; status = $Status; leg = 'A1'; machine_id = $machineId; classification = $Classification; content = $Content; code = $Code } | ConvertTo-Json -Compress
}
function Invoke-GitLines([string]$Repo, [string[]]$CommandArgs) {
    $lines = @(& git -C $Repo @CommandArgs 2>$null)
    if ($LASTEXITCODE -ne 0) { throw 'invalid' }
    return @($lines)
}
function Get-OneGitLine([string]$Repo, [string[]]$CommandArgs) {
    $lines = @(Invoke-GitLines -Repo $Repo -CommandArgs $CommandArgs)
    if ($lines.Count -ne 1) { throw 'invalid' }
    return ([string]$lines[0]).Trim()
}
function Invoke-GitQuiet([string]$Repo, [string[]]$CommandArgs) {
    $priorErrorAction = $ErrorActionPreference
    try { $ErrorActionPreference = 'Continue'; & git -C $Repo @CommandArgs 1>$null 2>$null; $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $priorErrorAction }
    if ($code -ne 0) { throw 'invalid' }
}
function Test-GitAncestor([string]$Repo, [string]$Ancestor, [string]$Descendant) {
    $priorErrorAction = $ErrorActionPreference
    try { $ErrorActionPreference = 'Continue'; & git -C $Repo merge-base --is-ancestor $Ancestor $Descendant 1>$null 2>$null; $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $priorErrorAction }
    if ($code -eq 0) { return $true }
    if ($code -eq 1) { return $false }
    throw 'invalid'
}
function Get-Json([string[]]$CommandArgs) {
    $output = @(& $python -m grim_dawn_sync --config $config --json @CommandArgs 2>$null)
    if ($LASTEXITCODE -ne 0) { throw 'invalid' }
    try { return ((@($output) -join [Environment]::NewLine) | ConvertFrom-Json -ErrorAction Stop) }
    catch { throw 'invalid' }
}
function Get-FileFingerprint([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return 'missing' }
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '') }
    finally { $sha.Dispose(); $stream.Dispose() }
}
function Get-FetchHeadFingerprint([string]$Vault) {
    # FETCH_HEAD is normally absent because the diagnostic network read uses
    # --no-write-fetch-head.  Its absence is nevertheless observable state:
    # a concurrent ordinary network refresh must fail closed like a ref move.
    $priorErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $lines = @(& git -C $Vault rev-parse --verify --quiet FETCH_HEAD 2>$null)
        $code = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $priorErrorAction }
    if ($code -eq 1) { return 'absent' }
    if ($code -ne 0 -or $lines.Count -ne 1 -or [string]$lines[0] -notmatch '^[0-9a-f]{40}(?:[0-9a-f]{24})?$') { throw 'invalid' }
    return ('present:' + [string]$lines[0])
}
function Get-LocalFingerprint([string]$Vault, [string]$State) {
    $sourceBranch = Get-OneGitLine -Repo $source -CommandArgs @('symbolic-ref','--quiet','HEAD')
    $sourceHead = Get-OneGitLine -Repo $source -CommandArgs @('rev-parse','HEAD')
    $sourceStatus = (Invoke-GitLines -Repo $source -CommandArgs @('status','--porcelain=v1','--untracked-files=all')) -join "`n"
    $vaultStatus = (Invoke-GitLines -Repo $Vault -CommandArgs @('status','--porcelain=v1','--untracked-files=all')) -join "`n"
    $vaultHead = Get-OneGitLine -Repo $Vault -CommandArgs @('rev-parse','HEAD')
    # Include every local ref, including refs/remotes, in a fixed order.  Do
    # not reveal the fingerprint or its components outside this process.
    $vaultRefs = (Invoke-GitLines -Repo $Vault -CommandArgs @('for-each-ref','--sort=refname','--format=%(refname) %(objectname)','refs')) -join "`n"
    $fetchHead = Get-FetchHeadFingerprint $Vault
    $parts = @()
    $parts += $sourceBranch
    $parts += $sourceHead
    $parts += $sourceStatus
    $parts += Get-FileFingerprint $python
    $parts += Get-FileFingerprint $config
    $parts += Get-FileFingerprint $State
    $parts += $vaultStatus
    $parts += $vaultHead
    $parts += $vaultRefs
    $parts += $fetchHead
    return ($parts -join "`0")
}

try {
    if (-not (Test-Path -LiteralPath $source -PathType Container) -or -not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $config -PathType Leaf) -or -not (Test-Path -LiteralPath $shortcut -PathType Leaf)) { throw 'invalid' }
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
    if ($link.TargetPath -cne [IO.Path]::GetFullPath($python) -or $link.WorkingDirectory -cne [IO.Path]::GetFullPath((Join-Path $source 'src')) -or $link.Arguments -notmatch 'grim_dawn_sync\.cli' -or $link.Arguments -notmatch "'launch'") { throw 'invalid' }
    $configured = Get-Content -LiteralPath $config -Raw -Encoding utf8 | ConvertFrom-Json -ErrorAction Stop
    if ($configured.machine_id -ne $machineId -or -not ($configured.vault_repo -is [string]) -or -not ($configured.vault_repo)) { throw 'invalid' }
    $vault = [string]$configured.vault_repo
    $state = Join-Path ([IO.Path]::GetDirectoryName($config)) 'state.json'
    if (-not (Test-Path -LiteralPath $vault -PathType Container)) { throw 'invalid' }
    $status = Get-Json -CommandArgs @('status'); $doctor = Get-Json -CommandArgs @('doctor')
    if ($status.schema_version -ne '1.0.0' -or $status.command -ne 'status' -or $doctor.schema_version -ne '1.0.0' -or $doctor.command -ne 'doctor' -or $doctor.read_only -ne $true -or $doctor.machine_id -ne $machineId -or $status.processes.complete -ne $true -or $status.processes.status -ne 'clear' -or $status.active_lock -ne $null -or $status.recovery_phase -ne $null -or $status.last_pushed_commit -notmatch $oidPattern) { throw 'invalid' }
    $beforeLocal = Get-LocalFingerprint $vault $state
    if (@(Invoke-GitLines -Repo $source -CommandArgs @('status','--porcelain=v1','--untracked-files=all')).Count -ne 0) { throw 'invalid' }
    $worktree = @(Invoke-GitLines -Repo $vault -CommandArgs @('status','--porcelain=v1','--untracked-files=all'))
    if ($worktree.Count -ne 0) { throw 'invalid' }
    $localHead = Get-OneGitLine -Repo $vault -CommandArgs @('rev-parse','HEAD')
    if ($localHead -cne [string]$status.last_pushed_commit) { throw 'invalid' }
    $beforeRemoteRefs = @(Invoke-GitLines -Repo $vault -CommandArgs @('ls-remote','--refs','origin','refs/heads/main','refs/tags/grim-dawn-sync-active'))
    $beforeMain = @($beforeRemoteRefs | Where-Object { $_ -match '^[0-9a-f]{40}(?:[0-9a-f]{24})?\s+refs/heads/main$' })
    if ($beforeMain.Count -ne 1) { throw 'invalid' }
    $remoteHead = ([string]$beforeMain[0] -split '\s+')[0]
    # Fetch the already advertised object ID, not the branch ref: clone's
    # default fetch refspec would otherwise advance refs/remotes/origin/main.
    Invoke-GitQuiet -Repo $vault -CommandArgs @('fetch','--no-tags','--no-write-fetch-head','origin',$remoteHead)
    $localIsAncestor = Test-GitAncestor $vault $localHead $remoteHead
    $remoteIsAncestor = Test-GitAncestor $vault $remoteHead $localHead
    $manifestRaw = @(& git -C $vault show "$remoteHead`:.sync/manifest.json" 2>$null) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw 'invalid' }
    $remoteManifest = $manifestRaw | ConvertFrom-Json -ErrorAction Stop
    $liveRoot = [string]$doctor.checks.save_root.manifest.root_hash
    if ($liveRoot -notmatch '^[0-9a-f]{64}$' -or $remoteManifest.root_hash -notmatch '^[0-9a-f]{64}$') { throw 'invalid' }
    $afterRemoteRefs = @(Invoke-GitLines -Repo $vault -CommandArgs @('ls-remote','--refs','origin','refs/heads/main','refs/tags/grim-dawn-sync-active'))
    $afterLocal = Get-LocalFingerprint $vault $state
    $afterDoctor = Get-Json -CommandArgs @('doctor')
    $afterLiveRoot = [string]$afterDoctor.checks.save_root.manifest.root_hash
    if (($beforeRemoteRefs -join "`n") -cne ($afterRemoteRefs -join "`n") -or $afterLocal -cne $beforeLocal -or $afterLiveRoot -cne $liveRoot) { Write-Output (Write-Classification 'blocked' 'unknown' 'unknown' 'observation_changed'); exit 1 }
    $classification = if ($remoteHead -ceq $localHead) { 'equal' } elseif ($localIsAncestor -and -not $remoteIsAncestor) { 'remote_ahead' } elseif ($remoteIsAncestor -and -not $localIsAncestor) { 'remote_behind' } else { 'diverged' }
    $content = if ($liveRoot -ceq $remoteManifest.root_hash) { 'same' } else { 'different' }
    # An advanced remote is still reported by ancestry even when its content differs;
    # only same content is safe to continue without an explicit user decision.
    $safe = ($classification -eq 'remote_ahead' -and $content -eq 'same')
    $code = if ($safe) { 'safe_remote_ahead' } elseif ($classification -eq 'remote_ahead' -and $content -eq 'different') { 'remote_content_differs' } else { 'not_target_relation' }
    Write-Output (Write-Classification $(if ($safe) { 'complete' } else { 'blocked' }) $classification $content $code); exit $(if ($safe) { 0 } else { 1 })
}
catch { Write-Output (Write-Classification 'blocked' 'unknown' 'unknown' 'observation_changed'); exit 1 }
```

## Copy-paste execution: validated remote diff summary

Run this deployed, local block only after the request block exits zero.  It
prints one aggregate-only sentinel: no path, character/account name, object
ID, root hash, URL, file content, or Git stderr is emitted.  The two commits
are fetched by their advertised object ID with `--no-tags --no-write-fetch-head`.
Both manifests are structurally validated and their declared save blobs are
checked against the commit before comparison.  Any changed local, live, or
advertised remote observation fails closed.

```powershell
$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
$toolRoot=Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool'; $python=Join-Path $toolRoot '.venv\Scripts\python.exe'
$config=Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSync\config.local.json'; $source=Join-Path $env:USERPROFILE 'grimdawnrep'; $machineId='desktop-a'
$oidPattern='^[0-9a-f]{40}(?:[0-9a-f]{24})?$'
function Out-Summary($Status,$Code,$Live,$Base,$Core,$Other,$Outside) {
  if($Status -ne 'complete') { $Status='blocked'; $Code='observation_changed'; $Live='unknown'; $Base='unknown'; $Core=$Other=$Outside=$null }
  $row=[ordered]@{sentinel='TERMINAL_A_REMOTE_DIFF_SUMMARY';status=$Status;leg='A1';machine_id=$machineId;code=$Code;live_vs_remote=$Live;baseline_vs_remote=$Base}
  foreach($entry in @(@('character_core',$Core),@('character_tree_other',$Other),@('outside_character_tree',$Outside))){
    if($null -eq $entry[1]){$row[$entry[0]]=[ordered]@{any_change=$false;added=0;removed=0;changed=0;changed_size_bucket='zero'}}
    else{$row[$entry[0]]=$entry[1]}}
  $row|ConvertTo-Json -Compress -Depth 4
}
function GitLines($Repo,[string[]]$CommandArgs){$x=@(& git -C $Repo @CommandArgs 2>$null);if($LASTEXITCODE -ne 0){throw 'invalid'};return @($x)}
function GitOne($Repo,[string[]]$CommandArgs){$x=@(GitLines -Repo $Repo -CommandArgs $CommandArgs);if($x.Count -ne 1){throw 'invalid'};return ([string]$x[0]).Trim()}
function GitQuiet($Repo,[string[]]$CommandArgs){$old=$ErrorActionPreference;try{$ErrorActionPreference='Continue';& git -C $Repo @CommandArgs 1>$null 2>$null;$rc=$LASTEXITCODE}finally{$ErrorActionPreference=$old};if($rc -ne 0){throw 'invalid'}}
function HashFile($Path){if(!(Test-Path -LiteralPath $Path -PathType Leaf)){return 'missing'};$s=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read);$h=[Security.Cryptography.SHA256]::Create();try{return([BitConverter]::ToString($h.ComputeHash($s))).Replace('-','')}finally{$h.Dispose();$s.Dispose()}}
 function Json([string[]]$CommandArgs){$x=@(& $python -m grim_dawn_sync --config $config --json @CommandArgs 2>$null);if($LASTEXITCODE -ne 0){throw 'invalid'};try{return (($x -join [Environment]::NewLine)|ConvertFrom-Json -ErrorAction Stop)}catch{throw 'invalid'}}
 function FetchMark($Vault){$p=GitOne -Repo $Vault -CommandArgs @('rev-parse','--git-path','FETCH_HEAD');if(![IO.Path]::IsPathRooted($p)){$p=Join-Path $Vault $p};return (HashFile $p)}
 function LocalMark($Vault,$State){return (@((GitOne -Repo $source -CommandArgs @('symbolic-ref','--quiet','HEAD')),(GitOne -Repo $source -CommandArgs @('rev-parse','HEAD')),((GitLines -Repo $source -CommandArgs @('status','--porcelain=v1','--untracked-files=all'))-join "`n"),(HashFile $python),(HashFile $config),(HashFile $State),((GitLines -Repo $Vault -CommandArgs @('status','--porcelain=v1','--untracked-files=all'))-join "`n"),(GitOne -Repo $Vault -CommandArgs @('rev-parse','HEAD')),((GitLines -Repo $Vault -CommandArgs @('for-each-ref','--sort=refname','--format=%(refname) %(objectname)','refs'))-join "`n"),(FetchMark $Vault)) -join "`0")}
function Manifest($Vault,$Commit){
  # This is the already installed, fixed local package only.  It is never
  # loaded from the fetched commit; validate_commit_snapshot recomputes the
  # root hash and verifies every declared blob plus save-tree exactness.
  $probe='from pathlib import Path; from grim_dawn_sync.git_vault import GitVault; import sys; GitVault(Path(sys.argv[1])).validate_commit_snapshot(sys.argv[2])'
  $prior=$ErrorActionPreference;try{$ErrorActionPreference='Continue';& $python -c $probe $Vault $Commit 1>$null 2>$null;$rc=$LASTEXITCODE}finally{$ErrorActionPreference=$prior};if($rc -ne 0){throw 'invalid'}
  $raw=@(& git -C $Vault show "$Commit`:.sync/manifest.json" 2>$null)-join "`n";if($LASTEXITCODE -ne 0 -or !$raw){throw 'invalid'}
  try{$m=$raw|ConvertFrom-Json -ErrorAction Stop}catch{throw 'invalid'}
  if($m.root_hash -notmatch '^[0-9a-f]{64}$' -or !($m.files -is [array]) -or $m.file_count -ne $m.files.Count){throw 'invalid'}
  return $m
}
function Bucket([int64]$Bytes){if($Bytes -eq 0){'zero'}elseif($Bytes -le 4096){'le_4k'}elseif($Bytes -le 65536){'le_64k'}elseif($Bytes -le 1048576){'le_1m'}else{'gt_1m'}}
function Category($Path){$p=$Path -replace '\\','/';if($p -imatch '^main/[^/]+/player\.gdc$'){'character_core'}elseif($p -imatch '^main/[^/]+/'){'character_tree_other'}else{'outside_character_tree'}}
function CompareManifests($Left,$Right){$l=@{};$r=@{};foreach($x in $Left.files){$l[$x.path]=$x};foreach($x in $Right.files){$r[$x.path]=$x};$out=@{};foreach($c in @('character_core','character_tree_other','outside_character_tree')){$out[$c]=[ordered]@{any_change=$false;added=0;removed=0;changed=0;changed_size_bucket='zero';_bytes=[int64]0}}
  $allPaths=New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase);foreach($p in $l.Keys){[void]$allPaths.Add([string]$p)};foreach($p in $r.Keys){[void]$allPaths.Add([string]$p)};foreach($p in @($allPaths|Sort-Object)){$a=$l[$p];$b=$r[$p];$kind=Category $p;$row=$out[$kind];if($null -eq $a){$row.added++;$row._bytes+=[int64]$b.size}elseif($null -eq $b){$row.removed++;$row._bytes+=[int64]$a.size}elseif($a.sha256 -cne $b.sha256 -or [int64]$a.size -ne [int64]$b.size){$row.changed++;$row._bytes+=[Math]::Max([int64]$a.size,[int64]$b.size)}}
  foreach($c in $out.Keys){$row=$out[$c];$row.any_change=(($row.added+$row.removed+$row.changed)-gt 0);$row.changed_size_bucket=Bucket $row._bytes;[void]$row.Remove('_bytes')};return $out}
try{
  if(!(Test-Path -LiteralPath $python -PathType Leaf) -or !(Test-Path -LiteralPath $config -PathType Leaf)){throw 'invalid'};$cfg=Get-Content -LiteralPath $config -Raw -Encoding utf8|ConvertFrom-Json -ErrorAction Stop;if($cfg.machine_id -cne $machineId -or !($cfg.vault_repo -is [string])){throw 'invalid'};$vault=[string]$cfg.vault_repo;$state=Join-Path ([IO.Path]::GetDirectoryName($config)) 'state.json'
  $status=Json -CommandArgs @('status');$doctor=Json -CommandArgs @('doctor');if($status.readiness -ne 'blocked' -or $status.vault_relation -ne 'remote_changed_or_unknown' -or $status.active_lock -ne $null -or $status.recovery_phase -ne $null -or $status.processes.status -ne 'clear' -or $doctor.machine_id -cne $machineId){throw 'invalid'}
  $before=LocalMark $vault $state;$baseline=GitOne -Repo $vault -CommandArgs @('rev-parse','HEAD');if($baseline -cne [string]$status.last_pushed_commit -or @(GitLines -Repo $vault -CommandArgs @('status','--porcelain=v1','--untracked-files=all')).Count){throw 'invalid'};$refs1=@(GitLines -Repo $vault -CommandArgs @('ls-remote','--refs','origin'));$main=@($refs1|Where-Object{$_ -match '^[0-9a-f]{40}(?:[0-9a-f]{24})?\s+refs/heads/main$'});if($main.Count -ne 1){throw 'invalid'};$remote=([string]$main[0]-split '\s+')[0];GitQuiet -Repo $vault -CommandArgs @('fetch','--no-tags','--no-write-fetch-head','origin',$remote);GitQuiet -Repo $vault -CommandArgs @('merge-base','--is-ancestor',$baseline,$remote)
  $baseManifest=Manifest $vault $baseline;$remoteManifest=Manifest $vault $remote;$summary=CompareManifests $baseManifest $remoteManifest;$live=[string]$doctor.checks.save_root.manifest.root_hash;if($live -notmatch '^[0-9a-f]{64}$'){throw 'invalid'};$refs2=@(GitLines -Repo $vault -CommandArgs @('ls-remote','--refs','origin'));$after=LocalMark $vault $state;$again=Json -CommandArgs @('doctor');if(($refs1-join "`n") -cne ($refs2-join "`n") -or $before -cne $after -or $live -cne [string]$again.checks.save_root.manifest.root_hash){throw 'invalid'};Out-Summary 'complete' 'remote_diff_summarized' $(if($live -ceq $remoteManifest.root_hash){'same'}else{'different'}) $(if($baseManifest.root_hash -ceq $remoteManifest.root_hash){'same'}else{'different'}) $summary.character_core $summary.character_tree_other $summary.outside_character_tree;exit 0
}catch{Write-Output (Out-Summary 'blocked' 'observation_changed' 'unknown' 'unknown' $null $null $null);exit 1}
```

The old failure-diagnosis block below is retained only as historical context;
do not run it for this request.

## Retired failure-diagnosis reference

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
