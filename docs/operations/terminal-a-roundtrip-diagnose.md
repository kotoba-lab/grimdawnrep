# Terminal A roundtrip: selector cancel/reload dry run

Use this runbook only after A1 reports a stable, content-different
`remote_ahead` observation and its aggregate diff summary. It exercises the
already enrolled Terminal A (`desktop-a`) selector without selecting data,
starting DPYes or Grim Dawn, or changing a save, state, configuration, source
worktree, or Vault ref (apart from the designated catalog tracking ref update
performed by the installed selector while it refreshes its catalog).

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
    source_policy = 'source_policy_invalid'
    user_skills = 'user_skills_invalid'
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
    $safeStages = @('origin_identity','source_branch','source_policy','user_skills','fingerprint','fetch','oid',
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
    $priorErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $lines = @(& git -C $source @GitArgs 2>$null)
        $gitExitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $priorErrorAction }
    if ($gitExitCode -ne 0) { throw 'invalid' }
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

function Assert-NoReparse([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'invalid' }
}

function Get-UserSkillTreeMark([string]$Root, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { throw 'invalid' }
    Assert-NoReparse $Root
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd([char[]]'\')
    $prefix = $rootFull + '\'
    $rows = New-Object 'System.Collections.Generic.List[string]'
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($rootFull)
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        Assert-NoReparse $current
        foreach ($item in @(Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop)) {
            $full = [IO.Path]::GetFullPath($item.FullName)
            if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'invalid' }
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'invalid' }
            $relative = $full.Substring($prefix.Length)
            if ([string]::IsNullOrEmpty($relative) -or $relative.StartsWith('\') -or
                $relative.StartsWith('/') -or $relative.Split('\') -contains '..') { throw 'invalid' }
            $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($relative))
            if ($item.PSIsContainer) {
                [void]$rows.Add($Label + [char]0 + 'D' + [char]0 + $encoded)
                $pending.Push($full)
            }
            else {
                $lengthBefore = [int64]$item.Length
                $digest = Get-FileSha256 $item.FullName
                $after = Get-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
                if (($after.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
                    [int64]$after.Length -ne $lengthBefore) { throw 'invalid' }
                [void]$rows.Add($Label + [char]0 + 'F' + [char]0 + $encoded + [char]0 +
                    [string]$lengthBefore + [char]0 + $digest)
            }
        }
    }
    if ($rows.Count -eq 0) { throw 'invalid' }
    $ordered = [string[]]$rows.ToArray()
    [Array]::Sort($ordered, [StringComparer]::Ordinal)
    return $ordered -join "`n"
}

function Assert-SourcePolicy {
    Invoke-GitQuiet @('diff','--quiet','--ignore-submodules=none','--')
    Invoke-GitQuiet @('diff','--cached','--quiet','--ignore-submodules=none','--')
    $status = @(Invoke-GitLines @('status','--porcelain=v1','--untracked-files=all'))
    foreach ($row in $status) { if (-not ([string]$row).StartsWith('?? ')) { throw 'invalid' } }
    $trackedInRoots = @(Invoke-GitLines @('ls-files','--',
        '.agents/skills/grim-dawn-buildcraft/**','.claude/skills/grim-dawn-buildcraft/**'))
    if ($trackedInRoots.Count -ne 0) { throw 'invalid' }
    $outside = @(Invoke-GitLines @('ls-files','--others','--exclude-standard',
        '--exclude=.agents/skills/grim-dawn-buildcraft/**',
        '--exclude=.claude/skills/grim-dawn-buildcraft/**','--','.'))
    if ($outside.Count -ne 0) { throw 'invalid' }
    $agentEntries = @(Invoke-GitLines @('ls-files','--others','--exclude-standard','--',
        '.agents/skills/grim-dawn-buildcraft/**'))
    $claudeEntries = @(Invoke-GitLines @('ls-files','--others','--exclude-standard','--',
        '.claude/skills/grim-dawn-buildcraft/**'))
    if ($agentEntries.Count -eq 0 -or $claudeEntries.Count -eq 0) { throw 'invalid' }
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

    $stage = 'source_policy'
    Assert-SourcePolicy

    $stage = 'user_skills'
    $agentsParent = Join-Path $source '.agents'
    $agentsSkills = Join-Path $agentsParent 'skills'
    $agentsRoot = Join-Path $agentsSkills 'grim-dawn-buildcraft'
    $claudeParent = Join-Path $source '.claude'
    $claudeSkills = Join-Path $claudeParent 'skills'
    $claudeRoot = Join-Path $claudeSkills 'grim-dawn-buildcraft'
    foreach ($path in @($source,$agentsParent,$agentsSkills,$agentsRoot,
        $claudeParent,$claudeSkills,$claudeRoot)) { Assert-NoReparse $path }
    $beforeAgentsSkill = Get-UserSkillTreeMark $agentsRoot 'agents'
    $beforeClaudeSkill = Get-UserSkillTreeMark $claudeRoot 'claude'

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
    $localPackageTree = Get-OneGitLine @('rev-parse',"$beforeHead`:src/grim_dawn_sync")
    $publicPackageTree = Get-OneGitLine @('rev-parse',"$fetchHead`:src/grim_dawn_sync")
    if ($localPackageTree -cnotmatch $oidPattern -or $publicPackageTree -cnotmatch $oidPattern -or
        $localPackageTree -cne $publicPackageTree) { throw 'invalid' }
    $requestCommit = $fetchHead

    $stage = 'blob'
    $requestLines = @(Invoke-GitLines @('show',"$requestCommit`:$requestObjectPath"))
    if ($requestLines.Count -eq 0) { throw 'invalid' }
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
        -not ($request.sequence -is [int]) -or $request.sequence -ne 13 -or
        -not ($request.target_machine_id -is [string]) -or $request.target_machine_id -cne $machineId -or
        -not ($request.leg -is [string]) -or -not ($request.observed_code -is [string]) -or
        -not ($request.action -is [string]) -or -not ($request.response_sentinel -is [string]) -or
        -not ($request.request_id -is [string]) -or
        $request.leg -cne 'A1' -or $request.observed_code -cne 'remote_changed_or_unknown' -or
        $request.action -cne 'source_path_runtime_repair' -or
        $request.response_sentinel -cne 'TERMINAL_A_SOURCE_PATH_REPAIR') { throw 'invalid' }
    Assert-ExactArray $request.checks @('validated_public_source_tree','preserved_exact_local_user_skill_roots','exact_pth_contract','post_repair_import_and_invariants')
    Assert-ExactArray $request.constraints @('fixed_local_pth_only','allow_exact_untracked_user_skill_roots_in_place','no_user_skill_mutation','no_pip_network_git_com_ui_input_process_kill_or_write',
        'no_source_config_state_save_vault_shortcut_remote_ref_mutation','no_fetched_code_execution','atomic_pth_write_with_exact_rollback')
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
    Assert-SourcePolicy
    $afterAgentsSkill = Get-UserSkillTreeMark $agentsRoot 'agents'
    $afterClaudeSkill = Get-UserSkillTreeMark $claudeRoot 'claude'
    $afterPython = Get-FileSha256 $python
    $afterConfig = Get-FileSha256 $config
    if ($afterBranch -cne $beforeBranch -or $afterHead -cne $beforeHead -or
        $afterAgentsSkill -cne $beforeAgentsSkill -or $afterClaudeSkill -cne $beforeClaudeSkill -or
        $afterPython -cne $beforePython -or $afterConfig -cne $beforeConfig) { throw 'invalid' }
    exit 0
}
catch {
    Write-Output (Write-RequestBlocked)
    exit 1
}
```

## Post-selector-failure stage probe (sequence 10)

Run this **installed local block only** after the sequence 10 request block
exits zero.  It is a bounded, read-only diagnosis: it does not open a shortcut,
touch a window, send input, fetch, alter refs, or change any pre-existing
process.  A timed-out native command is the sole exception: the block terminates
only the process tree that it started for that command.  Its output contains
only the failing stage and fixed allow-listed code; successful output adds only
aggregate relation, lock, process, and exact-title window buckets.

```powershell
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$toolRoot=Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool';$python=Join-Path $toolRoot '.venv\Scripts\python.exe'
$config=Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSync\config.local.json';$source=Join-Path $env:USERPROFILE 'grimdawnrep';$machineId='desktop-a';$selectorTitle='Grim Dawn Save Selection'
Add-Type @'
using System;using System.Text;using System.Collections.Generic;using System.Runtime.InteropServices;
public static class StageProbeWindow { delegate bool P(IntPtr h,IntPtr l); [DllImport("user32.dll")] static extern bool EnumWindows(P p,IntPtr l); [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h); [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr h,StringBuilder b,int n); public static int Count(string t){int n=0;EnumWindows(delegate(IntPtr h,IntPtr l){if(IsWindowVisible(h)){var b=new StringBuilder(512);GetWindowText(h,b,b.Capacity);if(String.Equals(b.ToString(),t,StringComparison.Ordinal)){n++;}}return true;},IntPtr.Zero);return n;} }
'@
function Bucket($n){if($null -eq $n){'unknown'}elseif($n -eq 0){'zero'}elseif($n -eq 1){'one'}else{'two_or_more'}}
function Digest($p){if(!(Test-Path -LiteralPath $p -PathType Leaf)){throw 'required_local_artifact_missing'};$h=[Security.Cryptography.SHA256]::Create();try{([BitConverter]::ToString($h.ComputeHash([IO.File]::ReadAllBytes($p)))).Replace('-','')}finally{$h.Dispose()}}
function TreeDigest($root){if(!(Test-Path -LiteralPath $root -PathType Container)){throw 'required_local_artifact_missing'};$base=[IO.Path]::GetFullPath($root).TrimEnd([char[]]'\')+'\';$rows=@();foreach($i in @(Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction Stop|Sort-Object FullName)){$full=[IO.Path]::GetFullPath($i.FullName);if(!$full.StartsWith($base,[StringComparison]::OrdinalIgnoreCase)){throw 'post_invariant_changed'};$rows+=$full.Substring($base.Length)+'='+$i.Length+'='+(Digest $full)};$h=[Security.Cryptography.SHA256]::Create();try{$bytes=[Text.Encoding]::UTF8.GetBytes(($rows-join"`n"));([BitConverter]::ToString($h.ComputeHash($bytes))).Replace('-','')}finally{$h.Dispose()}}
function ToolIdentity($p,$sourceRoot){$site=Join-Path ([IO.Path]::GetDirectoryName([IO.Path]::GetDirectoryName($p))) 'Lib\site-packages';$pth=Join-Path $site 'grim_dawn_sync_source.pth';$src=Join-Path $sourceRoot 'src';$entry=Join-Path $src 'grim_dawn_sync\__main__.py';if(!(Test-Path -LiteralPath $entry -PathType Leaf)){throw 'required_local_artifact_missing'};$actual=[IO.File]::ReadAllBytes($pth);$expected=(New-Object Text.UTF8Encoding($false)).GetBytes($src+[Environment]::NewLine);if($actual.Length-ne$expected.Length){throw 'required_local_artifact_missing'};for($i=0;$i-lt$actual.Length;$i++){if($actual[$i]-ne$expected[$i]){throw 'required_local_artifact_missing'}};$m=Get-Item -LiteralPath $p;([IO.Path]::GetFullPath($p))+[char]0+$m.Length+[char]0+$m.LastWriteTimeUtc.Ticks+[char]0+(Digest $pth)}
function Q($s){'"'+([string]$s).Replace('"','\"')+'"'}
function StopTree($targetPid){$psi=New-Object Diagnostics.ProcessStartInfo;$psi.FileName=(Join-Path $env:SystemRoot 'System32\taskkill.exe');$psi.Arguments='/PID '+[int]$targetPid+' /T /F';$psi.UseShellExecute=$false;$psi.CreateNoWindow=$true;$psi.RedirectStandardOutput=$true;$psi.RedirectStandardError=$true;$p=New-Object Diagnostics.Process;$p.StartInfo=$psi;if($p.Start()){[void]$p.WaitForExit(5000);if(!$p.HasExited){$p.Kill()}}}
function Native($f,[string[]]$a,$code){$psi=New-Object Diagnostics.ProcessStartInfo;$psi.FileName=$f;$psi.Arguments=(($a|ForEach-Object{Q $_})-join ' ');$psi.UseShellExecute=$false;$psi.CreateNoWindow=$true;$psi.RedirectStandardOutput=$true;$psi.RedirectStandardError=$true;$psi.EnvironmentVariables['GIT_TERMINAL_PROMPT']='0';$psi.EnvironmentVariables['GCM_INTERACTIVE']='Never';$p=New-Object Diagnostics.Process;$p.StartInfo=$psi;if(!$p.Start()){throw $code};$o=$p.StandardOutput.ReadToEndAsync();$e=$p.StandardError.ReadToEndAsync();if(!$p.WaitForExit(60000)){StopTree $p.Id;throw $code};[void]$e.GetAwaiter().GetResult();$v=$o.GetAwaiter().GetResult();if($p.ExitCode-ne 0){throw $code};@($v-split"`r?`n"|Where-Object{$_-ne''})}
function Git($r,[string[]]$a,$code){@(Native 'git' (@('-C',$r)+$a) $code)}
function RepoMark($r,$code){$a=@(Git $r @('rev-parse','HEAD') $code);$n=@(Git $r @('rev-parse','--symbolic-full-name','HEAD') $code);$b=@(Git $r @('status','--porcelain=v1','--untracked-files=all') $code);$c=@(Git $r @('for-each-ref','--sort=refname','--format=%(refname) %(objectname)') $code);if($a.Count-ne 1 -or $n.Count-ne 1){throw $code};$head=if($n[0]-eq'HEAD'){'DETACHED'}elseif($n[0]-match'^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*$'){'BRANCH='+$n[0]}else{throw $code};$fetch=Join-Path (Join-Path $r '.git') 'FETCH_HEAD';$f=if(Test-Path -LiteralPath $fetch -PathType Leaf){'present='+(Get-Content -LiteralPath $fetch -Raw -Encoding utf8)}else{'absent'};$h=[Security.Cryptography.SHA256]::Create();try{$text=([string]$a[0])+[char]0+$head+[char]0+($b-join"`n")+[char]0+($c-join"`n")+[char]0+$f;$bytes=[Text.Encoding]::UTF8.GetBytes($text);([BitConverter]::ToString($h.ComputeHash($bytes))).Replace('-','')}finally{$h.Dispose()}}
function Json($arg,$code){try{$r=@(Native $python @('-m','grim_dawn_sync','--config',$config,'--json',$arg) $code);if($r.Count-ne 1){throw $code};$r[0]|ConvertFrom-Json -ErrorAction Stop}catch{throw $code}}
function StatusProjection($v){if($null-eq$v -or $v.schema_version-cne'1.0.0' -or $v.command-cne'status' -or $v.readiness-isnot[string] -or $v.vault_relation-isnot[string] -or $null-eq$v.processes -or $v.processes.complete-ne$true -or $v.processes.status-isnot[string]){throw 'status_shape_invalid'};([string]$v.schema_version)+'|'+([string]$v.command)+'|'+([string]$v.readiness)+'|'+([string]$v.vault_relation)+'|'+[string]($null-ne$v.active_lock)+'|'+[string]($null-ne$v.recovery_phase)+'|'+[string]$v.processes.complete+'|'+[string]$v.processes.status}
function DoctorProjection($v){try{if($v.schema_version-cne'1.0.0' -or $v.command-cne'doctor' -or $v.read_only-ne$true -or $v.machine_id-isnot[string] -or $v.checks.save_root.manifest.root_hash-notmatch'^[0-9a-f]{64}$' -or $v.passed-ne$true){throw 'doctor_shape_invalid'};([string]$v.schema_version)+'|'+([string]$v.command)+'|'+[string]$v.read_only+'|'+[string]$v.machine_id+'|'+[string]$v.passed+'|'+[string]$v.checks.save_root.manifest.root_hash}catch{throw 'doctor_shape_invalid'}}
function InstalledRoot($root){$code='from pathlib import Path; import re,sys; from grim_dawn_sync.manifest import build_manifest; x=build_manifest(Path(sys.argv[1]),machine_id=sys.argv[2]).get("root_hash"); sys.exit(2) if not isinstance(x,str) or re.fullmatch(r"[0-9a-f]{64}",x) is None else print(x)';$v=@(Native $python @('-c',$code,$root,$machineId) 'installed_manifest_mismatch');if($v.Count-ne 1 -or $v[0]-notmatch '^[0-9a-f]{64}$'){throw 'installed_manifest_mismatch'};[string]$v[0]}
function Remote($vault){$r=@(Git $vault @('ls-remote','--refs','origin','refs/heads/main','refs/tags/grim-dawn-sync-active') 'remote_advertisement_invalid');$m=@($r|Where-Object{$_ -match '^[0-9a-f]{40}\trefs/heads/main$'});$l=@($r|Where-Object{$_ -match '^[0-9a-f]{40}\trefs/tags/grim-dawn-sync-active$'});if($m.Count-ne 1 -or $l.Count-ne 0 -or $r.Count-ne 1){throw 'remote_advertisement_invalid'};[pscustomobject]@{raw=$r-join"`n";lock='clear'}}
function Out($status,$stage,$code,$relation,$lock,$processes,$windows){$x=[ordered]@{sentinel='TERMINAL_A_POST_SELECTOR_FAILURE_STAGE_PROBE';status=$status;leg='A1';machine_id=$machineId;stage=$stage;code=$code};if($status-eq'complete'){$x.relation=$relation;$x.remote_lock=$lock;$x.processes=$processes;$x.selector_window_count_bucket=$windows};$x|ConvertTo-Json -Compress}
$stage='bootstrap';try{
 if(!(Test-Path -LiteralPath $python -PathType Leaf)){throw 'required_local_artifact_missing'}
 $stage='config';if(!(Test-Path -LiteralPath $config -PathType Leaf)){throw 'required_local_artifact_missing'};try{$cfg=Get-Content -LiteralPath $config -Raw -Encoding utf8|ConvertFrom-Json -ErrorAction Stop}catch{throw 'required_local_artifact_missing'};if($cfg.machine_id-cne$machineId -or $cfg.vault_repo -isnot [string] -or $cfg.save_root -isnot [string]){throw 'machine_id_unexpected'};$vault=[string]$cfg.vault_repo;$live=[string]$cfg.save_root;$state=Join-Path ([IO.Path]::GetDirectoryName($config)) 'state.json'
 $shortcut=Join-Path (Join-Path $env:USERPROFILE 'Desktop') 'Grim Dawn (DPYes + Save Selection).lnk'
 $stage='package';$before=@((ToolIdentity $python $source),(Digest $config),(Digest $state),(Digest $shortcut),(TreeDigest $live));$installed=InstalledRoot $live
 $stage='source';$beforeSource=RepoMark $source 'source_probe_failed'
 $stage='vault';$beforeVault=RepoMark $vault 'vault_probe_failed'
 $stage='status_first';$s1=Json 'status' 'status_command_failed';$sp1=StatusProjection $s1
 $stage='doctor_first';$d1=Json 'doctor' 'doctor_command_failed';$dp1=DoctorProjection $d1
 $stage='live_manifest';if($installed-cne[string]$d1.checks.save_root.manifest.root_hash){throw 'installed_manifest_mismatch'}
 $stage='remote_advertisement';$r1=Remote $vault;$r2=Remote $vault;if($r1.raw-cne$r2.raw){throw 'remote_advertisement_invalid'}
 $stage='process_window';try{$all=@(Get-CimInstance Win32_Process -ErrorAction Stop);$games=@($all|Where-Object{$_.Name-in@('Grim Dawn.exe','DPYes.exe')})}catch{throw 'process_observation_inconclusive'};if($games.Count-ne 0){throw 'process_status_unexpected'}
 $stage='status_second';$s2=Json 'status' 'status_command_failed';try{$sp2=StatusProjection $s2}catch{throw 'status_not_stable'};if($sp1-cne$sp2){throw 'status_not_stable'}
 $stage='doctor_second';$d2=Json 'doctor' 'doctor_command_failed';try{$dp2=DoctorProjection $d2}catch{throw 'doctor_not_stable'};if($dp1-cne$dp2){throw 'doctor_not_stable'}
 $stage='semantic';if($d1.machine_id-cne$machineId){throw 'machine_id_unexpected'};if($s1.readiness-notin@('blocked','ready')){throw 'status_readiness_unexpected'};if($s1.vault_relation-notin@('equal','remote_changed_or_unknown','unborn','remote_missing')){throw 'vault_relation_unexpected'};if($null-ne$s1.active_lock -or $null-ne$s1.recovery_phase){throw 'lock_or_recovery_present'}
 $stage='post_invariant';$after=@((ToolIdentity $python $source),(Digest $config),(Digest $state),(Digest $shortcut),(TreeDigest $live));$afterSource=RepoMark $source 'source_probe_failed';$afterVault=RepoMark $vault 'vault_probe_failed';$lateDoctor=Json 'doctor' 'doctor_command_failed';[void](DoctorProjection $lateDoctor);$lateInstalled=InstalledRoot $live;if((($before -join [char]0) -cne ($after -join [char]0)) -or ($beforeSource -cne $afterSource) -or ($beforeVault -cne $afterVault) -or ([string]$lateDoctor.checks.save_root.manifest.root_hash-cne[string]$d1.checks.save_root.manifest.root_hash) -or ($lateInstalled-cne$installed)){throw 'post_invariant_changed'}
 $finalRemote=Remote $vault;if($finalRemote.raw-cne$r1.raw){throw 'remote_advertisement_invalid'}
 $stage='final_process_window';try{$finalAll=@(Get-CimInstance Win32_Process -ErrorAction Stop);$finalGames=@($finalAll|Where-Object{$_.Name-in@('Grim Dawn.exe','DPYes.exe')});$wins=[StageProbeWindow]::Count($selectorTitle)}catch{throw 'process_observation_inconclusive'};if($finalGames.Count-ne 0){throw 'process_status_unexpected'}
 $relation=if($s1.vault_relation-eq'remote_changed_or_unknown'){'unknown'}else{[string]$s1.vault_relation};Write-Output (Out 'complete' 'complete' 'stage_probe_complete' $relation $finalRemote.lock 'clear' (Bucket $wins));exit 0
}catch{$code=[string]$_.Exception.Message;$allowed=@('required_local_artifact_missing','source_probe_failed','vault_probe_failed','status_command_failed','status_shape_invalid','doctor_command_failed','doctor_shape_invalid','installed_manifest_mismatch','status_not_stable','doctor_not_stable','status_readiness_unexpected','vault_relation_unexpected','lock_or_recovery_present','process_status_unexpected','machine_id_unexpected','remote_advertisement_invalid','process_observation_inconclusive','post_invariant_changed','unexpected_failed');if($code-notin$allowed){$code='unexpected_failed'};Write-Output (Out 'blocked' $stage $code $null $null $null $null);exit 1}
```

## Retired package artifact substage probe (sequence 11; DO NOT RUN)

Sequence 11 incorrectly treated an absent
`.venv\Lib\site-packages\grim_dawn_sync` directory as a missing deployment.
The supported deployment instead imports the verified worktree through
`purelib\grim_dawn_sync_source.pth`.  The reported
`installed_package_directory/artifact_missing` result was therefore a false
diagnostic.  The executable block has been removed; do not reconstruct or run
it.

## Source-path runtime repair (sequence 13)

Run this installed local block only after the sequence 13 request validation
block exits zero.  That validation proves the tracked local state is clean,
preserves the exact two user-owned untracked skill roots in place, and proves the local
`HEAD:src/grim_dawn_sync` tree is byte-identical to the fetched canonical
public tree without checking out or executing fetched content.  This block
uses the fixed venv interpreter to locate its `purelib` directory.  It makes no
change when `grim_dawn_sync_source.pth` already contains the exact official
UTF-8-without-BOM bytes (`<source>\src` plus `Environment.NewLine`).  Otherwise
it atomically replaces only that `.pth` file, verifies import with bytecode
writes disabled, and restores the exact old bytes (or deletes a newly created
file) if import fails.  It does not use pip, Git, networking, COM, shortcuts,
UI input, or process control, and does not read or modify config, state, saves,
Vault, or remote refs.

```powershell
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$machineId='desktop-a';$source=Join-Path $env:USERPROFILE 'grimdawnrep';$toolRoot=Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool'
$python=Join-Path $toolRoot '.venv\Scripts\python.exe';$sourceRoot=Join-Path $source 'src';$entry=Join-Path $sourceRoot 'grim_dawn_sync\__main__.py'
$stage='bootstrap';$temp=$null;$rollback=$null;$pth=$null;$changed=$false;$success=$false;$oldExists=$false;$oldBytes=$null;$expected=$null
function Out($status,$stage,$code){[ordered]@{sentinel='TERMINAL_A_SOURCE_PATH_REPAIR';status=$status;leg='A1';machine_id=$machineId;stage=$stage;code=$code}|ConvertTo-Json -Compress}
function SameBytes([byte[]]$a,[byte[]]$b){if($null-eq$a-or$null-eq$b-or$a.Length-ne$b.Length){return $false};for($i=0;$i-lt$a.Length;$i++){if($a[$i]-ne$b[$i]){return $false}};return $true}
function Digest($p){if(!(Test-Path -LiteralPath $p -PathType Leaf)){throw 'artifact_missing'};$h=[Security.Cryptography.SHA256]::Create();try{([BitConverter]::ToString($h.ComputeHash([IO.File]::ReadAllBytes($p)))).Replace('-','')}finally{$h.Dispose()}}
function TreeDigest($root){if(!(Test-Path -LiteralPath $root -PathType Container)){throw 'artifact_missing'};$base=[IO.Path]::GetFullPath($root).TrimEnd([char[]]'\')+'\';$rows=@();foreach($item in @(Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction Stop|Where-Object{$_.FullName-notmatch'[\\/]__pycache__[\\/]' -and $_.Extension-cne'.pyc'}|Sort-Object FullName)){$full=[IO.Path]::GetFullPath($item.FullName);if(!$full.StartsWith($base,[StringComparison]::OrdinalIgnoreCase)){throw 'artifact_unreadable'};$rows+=$full.Substring($base.Length)+'='+$item.Length+'='+(Digest $full)};$h=[Security.Cryptography.SHA256]::Create();try{([BitConverter]::ToString($h.ComputeHash([Text.Encoding]::UTF8.GetBytes($rows-join"`n")))).Replace('-','')}finally{$h.Dispose()}}
function Python([string[]]$a,$code){$prior=$ErrorActionPreference;try{$ErrorActionPreference='Continue';$lines=@(& $python @a 2>$null);$exitCode=$LASTEXITCODE}finally{$ErrorActionPreference=$prior};if($exitCode-ne0){throw $code};@($lines)}
function AtomicSet([byte[]]$bytes,[string]$target,[ref]$scratch,[ref]$backup){$scratch.Value=Join-Path ([IO.Path]::GetDirectoryName($target)) ('.grim_dawn_sync_source.'+[Guid]::NewGuid().ToString('N')+'.tmp');[IO.File]::WriteAllBytes($scratch.Value,$bytes);if(!(SameBytes ([IO.File]::ReadAllBytes($scratch.Value)) $bytes)){throw 'source_path_write_failed'};if(Test-Path -LiteralPath $target -PathType Leaf){$backup.Value=Join-Path ([IO.Path]::GetDirectoryName($target)) ('.grim_dawn_sync_source.'+[Guid]::NewGuid().ToString('N')+'.rollback');[IO.File]::Replace($scratch.Value,$target,$backup.Value)}elseif(Test-Path -LiteralPath $target){throw 'source_path_unreadable'}else{[IO.File]::Move($scratch.Value,$target)};$scratch.Value=$null}
try{
 $stage='bootstrap';if(!(Test-Path -LiteralPath $python -PathType Leaf)){throw 'python_missing'}
 $stage='source_package';if(!(Test-Path -LiteralPath $entry -PathType Leaf)){throw 'source_package_missing'};$sourceMark=TreeDigest $sourceRoot;$pythonMark=Digest $python
 $stage='purelib';$purelibLines=@(Python @('-B','-c','import sysconfig; print(sysconfig.get_path(chr(112)+chr(117)+chr(114)+chr(101)+chr(108)+chr(105)+chr(98)))') 'purelib_invalid');if($purelibLines.Count-ne1){throw 'purelib_invalid'};$purelib=[IO.Path]::GetFullPath(([string]$purelibLines[0]).Trim());$fixedPurelib=[IO.Path]::GetFullPath((Join-Path $toolRoot '.venv\Lib\site-packages'));if($purelib-cne$fixedPurelib-or!(Test-Path -LiteralPath $purelib -PathType Container)){throw 'purelib_invalid'}
 $stage='source_path';$pth=Join-Path $purelib 'grim_dawn_sync_source.pth';if(Test-Path -LiteralPath $pth){try{if(!(Test-Path -LiteralPath $pth -PathType Leaf)-or((Get-Item -LiteralPath $pth -Force).Attributes-band[IO.FileAttributes]::ReparsePoint)){throw 'source_path_unreadable'};$oldExists=$true;$oldBytes=[IO.File]::ReadAllBytes($pth)}catch{throw 'source_path_unreadable'}};$expected=(New-Object Text.UTF8Encoding($false)).GetBytes($sourceRoot+[Environment]::NewLine);$already=SameBytes $oldBytes $expected
 if(!$already){try{AtomicSet $expected $pth ([ref]$temp) ([ref]$rollback);$changed=$true}catch{throw 'source_path_write_failed'}}
 $stage='import';try{$importLines=@(Python @('-B','-c','from pathlib import Path; import grim_dawn_sync, grim_dawn_sync.__main__, sys; actual=Path(grim_dawn_sync.__file__).resolve(); expected=Path(sys.argv[1]).resolve(); raise SystemExit(0 if expected in actual.parents else 3)',(Join-Path $sourceRoot 'grim_dawn_sync')) 'source_package_import_failed');if($importLines.Count-ne0){throw 'source_package_import_failed'}}catch{throw 'source_package_import_failed'}
 $stage='post_invariant';if(!(SameBytes ([IO.File]::ReadAllBytes($pth)) $expected)-or(TreeDigest $sourceRoot)-cne$sourceMark-or(Digest $python)-cne$pythonMark){throw 'post_invariant_changed'}
 $success=$true;Write-Output (Out 'complete' 'complete' $(if($already){'pth_already_current'}else{'pth_repaired'}));exit 0
}catch{$failedStage=$stage;$code=[string]$_.Exception.Message;if($changed-and!$success){$stage='rollback';try{if(!(Test-Path -LiteralPath $pth -PathType Leaf)-or!(SameBytes ([IO.File]::ReadAllBytes($pth)) $expected)){throw 'rollback_failed'};if($oldExists){if(!$rollback-or!(Test-Path -LiteralPath $rollback -PathType Leaf)-or!(SameBytes ([IO.File]::ReadAllBytes($rollback)) $oldBytes)){throw 'rollback_failed'};$temp=Join-Path ([IO.Path]::GetDirectoryName($pth)) ('.grim_dawn_sync_source.'+[Guid]::NewGuid().ToString('N')+'.discard');[IO.File]::Replace($rollback,$pth,$temp);$rollback=$null;if(!(SameBytes ([IO.File]::ReadAllBytes($pth)) $oldBytes)){throw 'rollback_failed'}}else{$rollback=Join-Path ([IO.Path]::GetDirectoryName($pth)) ('.grim_dawn_sync_source.'+[Guid]::NewGuid().ToString('N')+'.rollback');[IO.File]::Move($pth,$rollback);if(Test-Path -LiteralPath $pth){throw 'rollback_failed'}};$changed=$false;$stage=$failedStage}catch{$code='rollback_failed'}};$allowed=@('python_missing','source_package_missing','purelib_invalid','source_path_unreadable','source_path_write_failed','source_package_import_failed','rollback_failed','post_invariant_changed','artifact_missing','artifact_unreadable','unexpected_failed');if($code-notin$allowed){$code='unexpected_failed'};Write-Output (Out 'blocked' $stage $code);exit 1}finally{if($temp-and(Test-Path -LiteralPath $temp -PathType Leaf)){[IO.File]::Delete($temp)};if($rollback-and(!$oldExists-or$success)-and(Test-Path -LiteralPath $rollback -PathType Leaf)){[IO.File]::Delete($rollback)}}
```

Report exactly the single JSON line.  `pth_already_current` and `pth_repaired`
are both successful, idempotent outcomes.  Any blocked result is fail-closed;
do not launch the shortcut or run another repair.

### Retired dynamic quarantine (DO NOT RUN)

The quarantine approach is retired.  It cannot satisfy source policy while a
second legitimate user-owned skill root remains under `.claude`, and rich-text
operator delivery also corrupted dotted filenames in an earlier attempt.
Sequence 13 instead preserves both exact roots in place and fingerprints their
complete dynamic contents before and after validation.  Do not reconstruct or
run any quarantine, restore, ignore, delete, stash, or commit block for them.

## Retired selector cancel/reload automation (sequence 8; DO NOT RUN)

Sequence 8 is retained below only as a non-executable failure record.  It has
a PowerShell parse defect and its generic `EnumWindows` delegate cannot be
marshaled by Windows PowerShell 5.1.  Do not copy, repair, or run this block.

For T7, an operator must perform the visible Esc/F5 actions manually.  Run the
sequence 10 read-only stage probe immediately before and after those actions to
verify the source, Vault, state, live-save, remote-advertisement, process, and
selector-window invariants.  Never select a row, activate Launch, or confirm a
dialog.

```text
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$toolRoot=Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool';$python=Join-Path $toolRoot '.venv\Scripts\python.exe'
$config=Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSync\config.local.json';$source=Join-Path $env:USERPROFILE 'grimdawnrep';$machineId='desktop-a'
$shortcut=Join-Path (Join-Path $env:USERPROFILE 'Desktop') 'Grim Dawn (DPYes + Save Selection).lnk';$selectorTitle='Grim Dawn Save Selection';$catalogRef='refs/remotes/origin/main'
Add-Type @'
using System; using System.Text; using System.Runtime.InteropServices;
public static class SelectorWindow {
 [DllImport("user32.dll")] public static extern bool EnumWindows(Func<IntPtr,IntPtr,bool> f,IntPtr l);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h,StringBuilder b,int n);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);
 [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
 [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
 [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h,uint m,IntPtr w,IntPtr l);
}
'@
function Out-DryRun($Status,$Code,$First,$Reload,$Second,$Default,$Count,$Game,$Mutated){
  $valid=$Status -eq 'complete';if(!$valid){$Status='blocked';if($Code -notin @('precondition_failed','selector_failed','game_started_unexpectedly','observation_changed','unexpected_failed')){$Code='unexpected_failed'};$First=$null;$Reload=$null;$Second=$null;$Default='unknown';$Count='unknown';$Game=$null;$Mutated=$null}
  [ordered]@{sentinel='TERMINAL_A_SELECTOR_DRY_RUN';status=$Status;leg='A1';machine_id=$machineId;observations_valid=$valid;first_cancelled=$First;reload_completed=$Reload;second_cancelled=$Second;selector_visible_count=$(if($valid){2}else{$null});default_role=$Default;candidate_count_bucket=$Count;game_started=$Game;mutations_detected=$Mutated;code=$Code}|ConvertTo-Json -Compress
}
function Json([string[]]$Args){$x=@(& $python -m grim_dawn_sync --config $config --json @Args 2>$null);if($LASTEXITCODE -ne 0){throw 'precondition_failed'};try{return (($x -join [Environment]::NewLine)|ConvertFrom-Json -ErrorAction Stop)}catch{throw 'precondition_failed'}}
function Git([string]$Repo,[string[]]$Args){$x=@(& git -C $Repo @Args 2>$null);if($LASTEXITCODE -ne 0){throw 'observation_changed'};return @($x)}
function One([string]$Repo,[string[]]$Args){$x=@(Git $Repo $Args);if($x.Count -ne 1){throw 'observation_changed'};return ([string]$x[0]).Trim()}
function Hash($Path){if(!(Test-Path -LiteralPath $Path -PathType Leaf)){return 'missing'};(Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash}
function RefMap($Vault){$m=[ordered]@{};foreach($line in @(Git $Vault @('for-each-ref','--sort=refname','--format=%(refname) %(objectname)','refs'))){$p=[string]$line -split ' ',2;if($p.Count -ne 2){throw 'observation_changed'};$m[$p[0]]=$p[1]};return $m}
function Stable($Vault,$Remote,$Base,$Before,$After){
  $r1=@(Git $Vault @('ls-remote','--refs','origin','refs/heads/main','refs/tags/grim-dawn-sync-active'))-join "`n";$r2=@(Git $Vault @('ls-remote','--refs','origin','refs/heads/main','refs/tags/grim-dawn-sync-active'))-join "`n";if($r1 -cne $r2){return $false}
  # The selector may refresh only its dedicated catalog tracking ref.  Its
  # final value must be the already advertised main object; an arbitrary ref
  # update (or a stale/missing final catalog ref) is never acceptable.
  if(($Before.Contains($catalogRef) -and $Before[$catalogRef] -cne $Base) -or !$After.Contains($catalogRef) -or $After[$catalogRef] -cne $Remote){return $false}
  foreach($k in $Before.Keys){if($k -cne $catalogRef -and (!$After.Contains($k) -or $Before[$k] -cne $After[$k])){return $false}};foreach($k in $After.Keys){if($k -cne $catalogRef -and !$Before.Contains($k)){return $false}}
  return $true
}
function GameRunning(){return @((Get-Process -Name 'Grim Dawn','DPYes' -ErrorAction SilentlyContinue)).Count -gt 0}
function PythonPids(){return @((Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction Stop|ForEach-Object{[uint32]$_.ProcessId}|Sort-Object -Unique)}
function SelectorWindows(){
  $rows=New-Object 'System.Collections.Generic.List[object]';$cb=[Func[IntPtr,IntPtr,bool]]{param($h,$l) if([SelectorWindow]::IsWindowVisible($h)){ $b=New-Object Text.StringBuilder 512;[void][SelectorWindow]::GetWindowText($h,$b,$b.Capacity);if($b.ToString() -ceq $selectorTitle){[uint32]$p=0;[void][SelectorWindow]::GetWindowThreadProcessId($h,[ref]$p);$rows.Add([pscustomobject]@{handle=$h;pid=$p})} };return $true};[void][SelectorWindow]::EnumWindows($cb,[IntPtr]::Zero);return @($rows)
}
function WaitSelector([int]$Seconds,[uint32[]]$BeforePids){$end=(Get-Date).AddSeconds($Seconds);while((Get-Date)-lt $end){if((GameRunning)){throw 'game_started_unexpectedly'};$windows=@(SelectorWindows);$now=@(PythonPids);$new=@($now|Where-Object{$BeforePids -notcontains $_});if($windows.Count -eq 1 -and $new.Count -eq 1 -and $windows[0].pid -eq $new[0]){return $windows[0]};if($windows.Count -gt 1 -or $new.Count -gt 1){throw 'selector_failed'};Start-Sleep -Milliseconds 250};throw 'selector_failed'}
function WaitGone($Window,[int]$Seconds){$end=(Get-Date).AddSeconds($Seconds);while((Get-Date)-lt $end){if((GameRunning)){throw 'game_started_unexpectedly'};if(![SelectorWindow]::IsWindow($Window.handle)){return};Start-Sleep -Milliseconds 250};throw 'selector_failed'}
function SendSelectorKey($Window,[string]$Key){if($Key -eq 'ESC'){$v=27}elseif($Key -eq 'F5'){$v=116}else{throw 'selector_failed'};if(![SelectorWindow]::SetForegroundWindow($Window.handle)){throw 'selector_failed'};if(![SelectorWindow]::PostMessage($Window.handle,0x100,[IntPtr]$v,[IntPtr]::Zero) -or ![SelectorWindow]::PostMessage($Window.handle,0x101,[IntPtr]$v,[IntPtr]::Zero)){throw 'selector_failed'}}
try{
  if(!(Test-Path -LiteralPath $python -PathType Leaf) -or !(Test-Path -LiteralPath $config -PathType Leaf) -or !(Test-Path -LiteralPath $shortcut -PathType Leaf)){throw 'precondition_failed'}
  $cfg=Get-Content -LiteralPath $config -Raw -Encoding utf8|ConvertFrom-Json -ErrorAction Stop;if($cfg.machine_id -cne $machineId -or !($cfg.vault_repo -is [string])){throw 'precondition_failed'};$vault=[string]$cfg.vault_repo;$state=Join-Path ([IO.Path]::GetDirectoryName($config)) 'state.json'
  $sourceRoot=Join-Path $source 'src';$link=(New-Object -ComObject WScript.Shell).CreateShortcut($shortcut);$expectedArgs=@(& $python -c "from pathlib import Path; from grim_dawn_sync.shortcut import _launch_arguments; import sys; print(_launch_arguments(Path(sys.argv[1]),Path(sys.argv[2])))" $sourceRoot $config 2>$null);if($LASTEXITCODE -ne 0 -or $expectedArgs.Count -ne 1 -or $link.TargetPath -cne [IO.Path]::GetFullPath($python) -or $link.WorkingDirectory -cne [IO.Path]::GetFullPath($sourceRoot) -or $link.Arguments -cne [string]$expectedArgs[0]){throw 'precondition_failed'}
  $s=Json @('status');$d=Json @('doctor');if($s.readiness -ne 'blocked' -or $s.vault_relation -ne 'remote_changed_or_unknown' -or $s.active_lock -ne $null -or $s.recovery_phase -ne $null -or $s.processes.status -ne 'clear' -or $d.machine_id -cne $machineId -or (GameRunning)){throw 'precondition_failed'}
  $base=One $vault @('rev-parse','HEAD');if($base -cne [string]$s.last_pushed_commit){throw 'precondition_failed'};$r=@(Git $vault @('ls-remote','--refs','origin','refs/heads/main'));if($r.Count -ne 1){throw 'precondition_failed'};$remote=([string]$r[0]-split '\s+')[0]
  & git -C $vault fetch --no-tags --no-write-fetch-head origin $remote 1>$null 2>$null;if($LASTEXITCODE -ne 0){throw 'precondition_failed'};& git -C $vault merge-base --is-ancestor $base $remote 1>$null 2>$null;if($LASTEXITCODE -ne 0){throw 'precondition_failed'}
  $live=[string]$d.checks.save_root.manifest.root_hash;$baseRoot=([string]@(& git -C $vault show "$base`:.sync/manifest.json" 2>$null)-join "`n"|ConvertFrom-Json -ErrorAction Stop).root_hash;$remoteRoot=([string]@(& git -C $vault show "$remote`:.sync/manifest.json" 2>$null)-join "`n"|ConvertFrom-Json -ErrorAction Stop).root_hash;if($live -cne $baseRoot -or $remoteRoot -ceq $baseRoot){throw 'precondition_failed'}
  $versions=Json @('versions');if($versions.command -ne 'versions' -or !($versions.candidates -is [array])){throw 'precondition_failed'};$n=@($versions.candidates).Count;$bucket=if($n -le 1){'one'}elseif($n -eq 2){'two'}else{'three_or_more'}
  $before=@((One $source @('symbolic-ref','--quiet','HEAD')),(One $source @('rev-parse','HEAD')),((Git $source @('status','--porcelain=v1','--untracked-files=all'))-join "`n"),(Hash $python),(Hash $config),(Hash $state),((Git $vault @('status','--porcelain=v1','--untracked-files=all'))-join "`n"));$refsBefore=RefMap $vault
  if(@(SelectorWindows).Count -ne 0){throw 'precondition_failed'};$sh=New-Object -ComObject WScript.Shell;$pids=@(PythonPids);$null=$sh.Run('"'+$shortcut+'"',1,$false);$ui=WaitSelector 75 $pids;SendSelectorKey $ui 'ESC';WaitGone $ui 15;$first=$true
  if(@(SelectorWindows).Count -ne 0){throw 'selector_failed'};$pids=@(PythonPids);$null=$sh.Run('"'+$shortcut+'"',1,$false);$ui=WaitSelector 75 $pids;SendSelectorKey $ui 'F5';WaitGone $ui 15;$ui=WaitSelector 75 $pids;SendSelectorKey $ui 'ESC';WaitGone $ui 15;$reload=$true;$second=$true
  $refsAfter=RefMap $vault;$after=@((One $source @('symbolic-ref','--quiet','HEAD')),(One $source @('rev-parse','HEAD')),((Git $source @('status','--porcelain=v1','--untracked-files=all'))-join "`n"),(Hash $python),(Hash $config),(Hash $state),((Git $vault @('status','--porcelain=v1','--untracked-files=all'))-join "`n"));$again=Json @('doctor')
  if((GameRunning) -or ($before-join "`0") -cne ($after-join "`0") -or !$(Stable $vault $remote $base $refsBefore $refsAfter) -or $live -cne [string]$again.checks.save_root.manifest.root_hash){throw 'observation_changed'}
  Out-DryRun 'complete' 'selector_dry_run_complete' $first $reload $second 'remote_current' $bucket $false $false;exit 0
}catch{ $code=[string]$_.Exception.Message;if($code -notin @('precondition_failed','selector_failed','game_started_unexpectedly','observation_changed')){$code='unexpected_failed'};Write-Output (Out-DryRun 'blocked' $code $false $false $false 'unknown' 'one' ($code -eq 'game_started_unexpectedly') ($code -eq 'observation_changed'));exit 1 }
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
    $priorErrorAction = $ErrorActionPreference
    try { $ErrorActionPreference = 'Continue'; $lines = @(& git -C $Repo @CommandArgs 2>$null); $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $priorErrorAction }
    if ($code -ne 0) { throw 'invalid' }
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
    $priorErrorAction = $ErrorActionPreference
    try { $ErrorActionPreference = 'Continue'; $output = @(& $python -m grim_dawn_sync --config $config --json @CommandArgs 2>$null); $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $priorErrorAction }
    if ($code -ne 0) { throw 'invalid' }
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
    $manifestRaw = @(Invoke-GitLines -Repo $vault -CommandArgs @('show',"$remoteHead`:.sync/manifest.json")) -join "`n"
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
function GitLines($Repo,[string[]]$CommandArgs){$old=$ErrorActionPreference;try{$ErrorActionPreference='Continue';$x=@(& git -C $Repo @CommandArgs 2>$null);$rc=$LASTEXITCODE}finally{$ErrorActionPreference=$old};if($rc -ne 0){throw 'invalid'};return @($x)}
function GitOne($Repo,[string[]]$CommandArgs){$x=@(GitLines -Repo $Repo -CommandArgs $CommandArgs);if($x.Count -ne 1){throw 'invalid'};return ([string]$x[0]).Trim()}
function GitQuiet($Repo,[string[]]$CommandArgs){$old=$ErrorActionPreference;try{$ErrorActionPreference='Continue';& git -C $Repo @CommandArgs 1>$null 2>$null;$rc=$LASTEXITCODE}finally{$ErrorActionPreference=$old};if($rc -ne 0){throw 'invalid'}}
function HashFile($Path){if(!(Test-Path -LiteralPath $Path -PathType Leaf)){return 'missing'};$s=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read);$h=[Security.Cryptography.SHA256]::Create();try{return([BitConverter]::ToString($h.ComputeHash($s))).Replace('-','')}finally{$h.Dispose();$s.Dispose()}}
 function Json([string[]]$CommandArgs){$old=$ErrorActionPreference;try{$ErrorActionPreference='Continue';$x=@(& $python -m grim_dawn_sync --config $config --json @CommandArgs 2>$null);$rc=$LASTEXITCODE}finally{$ErrorActionPreference=$old};if($rc -ne 0){throw 'invalid'};try{return (($x -join [Environment]::NewLine)|ConvertFrom-Json -ErrorAction Stop)}catch{throw 'invalid'}}
 function FetchMark($Vault){$p=GitOne -Repo $Vault -CommandArgs @('rev-parse','--git-path','FETCH_HEAD');if(![IO.Path]::IsPathRooted($p)){$p=Join-Path $Vault $p};return (HashFile $p)}
 function LocalMark($Vault,$State){return (@((GitOne -Repo $source -CommandArgs @('symbolic-ref','--quiet','HEAD')),(GitOne -Repo $source -CommandArgs @('rev-parse','HEAD')),((GitLines -Repo $source -CommandArgs @('status','--porcelain=v1','--untracked-files=all'))-join "`n"),(HashFile $python),(HashFile $config),(HashFile $State),((GitLines -Repo $Vault -CommandArgs @('status','--porcelain=v1','--untracked-files=all'))-join "`n"),(GitOne -Repo $Vault -CommandArgs @('rev-parse','HEAD')),((GitLines -Repo $Vault -CommandArgs @('for-each-ref','--sort=refname','--format=%(refname) %(objectname)','refs'))-join "`n"),(FetchMark $Vault)) -join "`0")}
function Manifest($Vault,$Commit){
  # This is the already installed, fixed local package only.  It is never
  # loaded from the fetched commit; validate_commit_snapshot recomputes the
  # root hash and verifies every declared blob plus save-tree exactness.
  $probe='from pathlib import Path; from grim_dawn_sync.git_vault import GitVault; import sys; GitVault(Path(sys.argv[1])).validate_commit_snapshot(sys.argv[2])'
  $prior=$ErrorActionPreference;try{$ErrorActionPreference='Continue';& $python -c $probe $Vault $Commit 1>$null 2>$null;$rc=$LASTEXITCODE}finally{$ErrorActionPreference=$prior};if($rc -ne 0){throw 'invalid'}
  $raw=@(GitLines -Repo $Vault -CommandArgs @('show',"$Commit`:.sync/manifest.json"))-join "`n";if(!$raw){throw 'invalid'}
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

## Post-selector-failure readonly probe

Run this **installed local block only** after the sequence 9 request block
exits zero.  It neither opens the shortcut nor sends input.  It never fetches,
closes or kills a pre-existing process, and it does not write any source,
Vault, state, configuration, save, or remote ref.  A timed-out native command
is cleaned up by terminating only the process tree started for that command.
A residual selector is reported and left untouched.  Blocked output contains
only the decision and allow-listed code; aggregate observations are emitted
only after the complete path validates them.

```powershell
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
$toolRoot=Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool';$python=Join-Path $toolRoot '.venv\Scripts\python.exe'
$config=Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSync\config.local.json';$source=Join-Path $env:USERPROFILE 'grimdawnrep';$machineId='desktop-a';$selectorTitle='Grim Dawn Save Selection'
Add-Type @'
using System;using System.Text;using System.Collections.Generic;using System.Runtime.InteropServices;
public static class ProbeWindow { delegate bool WndProc(IntPtr h,IntPtr l); [DllImport("user32.dll")] static extern bool EnumWindows(WndProc f,IntPtr l); [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h); [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr h,StringBuilder b,int n); [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h,out uint p); public static uint[] Matching(string title){var rows=new List<uint>();EnumWindows(delegate(IntPtr h,IntPtr l){if(IsWindowVisible(h)){var b=new StringBuilder(512);GetWindowText(h,b,b.Capacity);if(String.Equals(b.ToString(),title,StringComparison.Ordinal)){uint p;GetWindowThreadProcessId(h,out p);rows.Add(p);}}return true;},IntPtr.Zero);return rows.ToArray();} }
'@
function Bucket($n){if($null -eq $n){return 'unknown'};if($n -eq 0){return 'zero'};if($n -eq 1){return 'one'};return 'two_or_more'}
function FileDigest([string]$p){if(!(Test-Path -LiteralPath $p -PathType Leaf)){throw 'precondition_failed'};$h=[Security.Cryptography.SHA256]::Create();try{return ([BitConverter]::ToString($h.ComputeHash([IO.File]::ReadAllBytes($p)))).Replace('-','')}finally{$h.Dispose()}}
function TreeDigest([string]$root){
  if(!(Test-Path -LiteralPath $root -PathType Container)){throw 'precondition_failed'}
  $base=[IO.Path]::GetFullPath($root).TrimEnd([char[]]'\')+'\'
  $rows=@()
  foreach($item in @(Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction Stop|Sort-Object FullName)){
    $full=[IO.Path]::GetFullPath($item.FullName)
    if(!$full.StartsWith($base,[StringComparison]::OrdinalIgnoreCase)){throw 'precondition_failed'}
    $rows += $full.Substring($base.Length)+'='+$item.Length+'='+(FileDigest $full)
  }
  $h=[Security.Cryptography.SHA256]::Create()
  try{$bytes=[Text.Encoding]::UTF8.GetBytes(($rows -join "`n"));$digest=$h.ComputeHash($bytes);return [BitConverter]::ToString($digest).Replace('-','')}
  finally{$h.Dispose()}
}
function Q($s){return '"'+([string]$s).Replace('"','\"')+'"'}
function StopTree($targetPid){$psi=New-Object Diagnostics.ProcessStartInfo;$psi.FileName=(Join-Path $env:SystemRoot 'System32\taskkill.exe');$psi.Arguments='/PID '+[int]$targetPid+' /T /F';$psi.UseShellExecute=$false;$psi.CreateNoWindow=$true;$psi.RedirectStandardOutput=$true;$psi.RedirectStandardError=$true;$p=New-Object Diagnostics.Process;$p.StartInfo=$psi;if($p.Start()){[void]$p.WaitForExit(5000);if(!$p.HasExited){$p.Kill()}}}
function Native($file,[string[]]$argv,$failure,$timeoutMs){$out=[IO.Path]::GetTempFileName();$err=[IO.Path]::GetTempFileName();try{$psi=New-Object Diagnostics.ProcessStartInfo;$psi.FileName=$file;$psi.Arguments=(($argv|ForEach-Object{Q $_}) -join ' ');$psi.UseShellExecute=$false;$psi.CreateNoWindow=$true;$psi.RedirectStandardOutput=$true;$psi.RedirectStandardError=$true;$psi.EnvironmentVariables['GIT_TERMINAL_PROMPT']='0';$psi.EnvironmentVariables['GCM_INTERACTIVE']='Never';$p=New-Object Diagnostics.Process;$p.StartInfo=$psi;if(!$p.Start()){throw $failure};$ot=$p.StandardOutput.ReadToEndAsync();$et=$p.StandardError.ReadToEndAsync();if(!$p.WaitForExit($timeoutMs)){StopTree $p.Id;throw $failure};$o=$ot.GetAwaiter().GetResult();$e=$et.GetAwaiter().GetResult();[IO.File]::WriteAllText($out,$o);[IO.File]::WriteAllText($err,$e);if($p.ExitCode -ne 0){throw $failure};return @($o -split "`r?`n"|Where-Object{$_ -ne ''})}finally{Remove-Item -LiteralPath $out,$err -Force -ErrorAction SilentlyContinue}}
function Git($r,[string[]]$a){$timeout=15000;if($a -contains 'ls-remote'){$timeout=60000};return @(Native 'git' (@('-C',$r)+$a) 'observation_changed' $timeout)}
function One($r,[string[]]$a){$v=@(Git $r $a);if($v.Count -ne 1){throw 'observation_changed'};return ([string]$v[0]).Trim()}
function RepoFingerprint($r){
  $head=One $r @('rev-parse','HEAD')
  $headName=One $r @('rev-parse','--symbolic-full-name','HEAD')
  if($headName -eq 'HEAD'){$headState='DETACHED'}elseif($headName -match '^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*$'){$headState='BRANCH='+$headName}else{throw 'observation_changed'}
  $status=(Git $r @('status','--porcelain=v1','--untracked-files=all')-join "`n")
   $refs=(Git $r @('for-each-ref','--sort=refname','--format=%(refname) %(objectname)')-join "`n")
  $fetch=Join-Path (Join-Path $r '.git') 'FETCH_HEAD'
  $fetchState=if(Test-Path -LiteralPath $fetch -PathType Leaf){'present='+((Get-Content -LiteralPath $fetch -Raw -Encoding utf8))}else{'absent'}
   [string[]]$parts=@($head,$headState,$status,$refs,$fetchState);return [string]::Join([char]0,$parts)
}
function RemoteAdvertisement($r){
  $rows=@(Git $r @('ls-remote','--refs','origin','refs/heads/main','refs/tags/grim-dawn-sync-active'))
  $main=@($rows|Where-Object{$_ -match '^[0-9a-f]{40}\trefs/heads/main$'})
  $lock=@($rows|Where-Object{$_ -match '^[0-9a-f]{40}\trefs/tags/grim-dawn-sync-active$'})
  if($main.Count -ne 1 -or $lock.Count -ne 0 -or $rows.Count -ne 1){throw 'observation_changed'}
  return ($rows -join "`n")
}
function Json($command){$v=@(Native $python @('-m','grim_dawn_sync','--config',$config,'--json',$command) 'precondition_failed' 60000);return (($v -join "`n")|ConvertFrom-Json -ErrorAction Stop)}
function StatusProjection($v){if($null-eq$v -or $v.schema_version-cne'1.0.0' -or $v.command-cne'status' -or $v.readiness-isnot[string] -or $v.vault_relation-isnot[string] -or $v.last_pushed_commit-isnot[string] -or $null-eq$v.processes -or $v.processes.complete-ne$true -or $v.processes.status-isnot[string]){throw 'precondition_failed'};([string]$v.schema_version)+'|'+([string]$v.command)+'|'+([string]$v.readiness)+'|'+([string]$v.vault_relation)+'|'+([string]$v.last_pushed_commit)+'|'+[string]($null-ne$v.active_lock)+'|'+[string]($null-ne$v.recovery_phase)+'|'+[string]$v.processes.complete+'|'+[string]$v.processes.status}
function DoctorProjection($v){if($null-eq$v -or $v.schema_version-cne'1.0.0' -or $v.command-cne'doctor' -or $v.read_only-ne$true -or $v.machine_id-isnot[string] -or $v.passed-ne$true -or $v.checks.save_root.manifest.root_hash-notmatch'^[0-9a-f]{64}$'){throw 'precondition_failed'};([string]$v.schema_version)+'|'+([string]$v.command)+'|'+[string]$v.read_only+'|'+([string]$v.machine_id)+'|'+[string]$v.passed+'|'+([string]$v.checks.save_root.manifest.root_hash)}
function InstalledManifestRoot($root){
  # This invokes the fixed installed package, never public source code.
  $probe='from pathlib import Path; import re,sys; from grim_dawn_sync.manifest import build_manifest; v=build_manifest(Path(sys.argv[1]),machine_id=sys.argv[2]).get("root_hash"); sys.exit(2) if not isinstance(v,str) or re.fullmatch(r"[0-9a-f]{64}",v) is None else print(v)'
  $v=@(Native $python @('-c',$probe,$root,$machineId) 'precondition_failed' 60000)
  if($v.Count -ne 1 -or $v[0] -notmatch '^[0-9a-f]{64}$'){throw 'precondition_failed'}
  return [string]$v[0]
}
function ToolIdentity($p){if(!(Test-Path -LiteralPath $p -PathType Leaf)){throw 'precondition_failed'};$site=Join-Path ([IO.Path]::GetDirectoryName([IO.Path]::GetDirectoryName($p))) 'Lib\site-packages';$pth=Join-Path $site 'grim_dawn_sync_source.pth';$src=Join-Path $source 'src';$entry=Join-Path $src 'grim_dawn_sync\__main__.py';if(!(Test-Path -LiteralPath $entry -PathType Leaf)-or!(Test-Path -LiteralPath $pth -PathType Leaf)){throw 'precondition_failed'};$actual=[IO.File]::ReadAllBytes($pth);$expected=(New-Object Text.UTF8Encoding($false)).GetBytes($src+[Environment]::NewLine);if($actual.Length-ne$expected.Length){throw 'precondition_failed'};for($i=0;$i-lt$actual.Length;$i++){if($actual[$i]-ne$expected[$i]){throw 'precondition_failed'}};$meta=Get-Item -LiteralPath $p;return @([IO.Path]::GetFullPath($p),$meta.Length,$meta.LastWriteTimeUtc.Ticks,(FileDigest -p $pth)) -join "`0"}
function Windows(){try{return @([ProbeWindow]::Matching($selectorTitle)|ForEach-Object{[pscustomobject]@{pid=$_}})}catch{throw 'process_observation_inconclusive'}}
function Processes(){try{return @(Get-CimInstance Win32_Process -ErrorAction Stop)}catch{throw 'process_observation_inconclusive'}}
function Out($status,$code,$safe,$selector,$owned,$py,$ps,$unchanged){$x=[ordered]@{sentinel='TERMINAL_A_POST_SELECTOR_FAILURE_PROBE';status=$status;leg='A1';machine_id=$machineId;safe_to_retry=$safe;code=$code};if($status-eq'complete'){$x.status_expected=$true;$x.lock_clear=$true;$x.recovery_clear=$true;$x.game_processes_clear=$true;$x.remote_lock_clear=$true;$x.remote_stable=$true;$x.live_unchanged=$unchanged;$x.state_unchanged=$unchanged;$x.vault_unchanged=$unchanged;$x.source_unchanged=$unchanged;$x.selector_window_count_bucket=(Bucket $selector);$x.selector_owned_python_count_bucket=(Bucket $owned);$x.all_python_count_bucket=(Bucket $py);$x.non_operator_powershell_count_bucket=(Bucket $ps)};$x|ConvertTo-Json -Compress}
try {
 $stage='paths';if(!(Test-Path $python -PathType Leaf) -or !(Test-Path $config -PathType Leaf)){throw 'precondition_failed'};$cfg=Get-Content $config -Raw -Encoding utf8|ConvertFrom-Json -ErrorAction Stop;if($cfg.machine_id -cne $machineId -or !($cfg.vault_repo -is [string]) -or !($cfg.save_root -is [string])){throw 'precondition_failed'};$vault=[string]$cfg.vault_repo;$live=[string]$cfg.save_root;$state=Join-Path ([IO.Path]::GetDirectoryName($config)) 'state.json';$shortcut=Join-Path (Join-Path $env:USERPROFILE 'Desktop') 'Grim Dawn (DPYes + Save Selection).lnk'
 $stage='before';
 [string]$beforeSource=RepoFingerprint $source;[string]$beforeVault=RepoFingerprint $vault;$before=@((ToolIdentity $python),(FileDigest $config),(FileDigest $state),(FileDigest $shortcut),(TreeDigest $live),$beforeSource,$beforeVault)
 $stage='cli';$s1=Json 'status';$sp1=StatusProjection $s1;$d1=Json 'doctor';$dp1=DoctorProjection $d1;$installedRoot1=InstalledManifestRoot $live;$s2=Json 'status';$sp2=StatusProjection $s2;$d2=Json 'doctor';$dp2=DoctorProjection $d2;$installedRoot2=InstalledManifestRoot $live;$liveRoot=[string]$d1.checks.save_root.manifest.root_hash;if($liveRoot -cne $installedRoot1 -or $liveRoot -cne $installedRoot2 -or $sp1-cne$sp2 -or $dp1-cne$dp2 -or $s1.readiness -ne 'blocked' -or $s1.vault_relation -ne 'remote_changed_or_unknown' -or $s1.active_lock -ne $null -or $s1.recovery_phase -ne $null){throw 'precondition_failed'}
 $r1=RemoteAdvertisement $vault;$r2=RemoteAdvertisement $vault;if($r1 -cne $r2){throw 'observation_changed'}
 $stage='processes';$proc=Processes;$game=@($proc|Where-Object{$_.Name -in @('Grim Dawn.exe','DPYes.exe')});$wins=@(Windows);$pys=@($proc|Where-Object{$_.Name -in @('python.exe','pythonw.exe')});$owned=@($wins|Where-Object{$pys.ProcessId -contains $_.pid});$otherPs=@($proc|Where-Object{$_.Name -in @('powershell.exe','pwsh.exe') -and $_.CommandLine -notmatch 'terminal-a-roundtrip-diagnose'})
 $stage='after';
 [string]$afterSource=RepoFingerprint $source;[string]$afterVault=RepoFingerprint $vault;$after=@((ToolIdentity $python),(FileDigest $config),(FileDigest $state),(FileDigest $shortcut),(TreeDigest $live),$afterSource,$afterVault);$afterDoctor=Json 'doctor';$installedRoot3=InstalledManifestRoot $live;$beforeMark=[string]::Join([char]0,@($before|ForEach-Object{[string]$_}));$afterMark=[string]::Join([char]0,@($after|ForEach-Object{[string]$_}));if(($beforeVault -cne $afterVault) -or ($beforeSource -cne $afterSource) -or ([string]$afterDoctor.checks.save_root.manifest.root_hash -cne $liveRoot) -or ($installedRoot3 -cne $liveRoot) -or ((TreeDigest $live) -cne $before[4]) -or ($beforeMark -cne $afterMark)){throw 'observation_changed'}
 if($game.Count -ne 0){throw 'precondition_failed'};if($wins.Count -ne 0){Write-Output (Out 'blocked' 'selector_residual_detected' $false $wins.Count $owned.Count $pys.Count $otherPs.Count $true);exit 1};Write-Output (Out 'complete' 'post_failure_probe_complete' $true 0 0 $pys.Count $otherPs.Count $true);exit 0
} catch { $code=$_.Exception.Message;if($code -notin @('precondition_failed','observation_changed','process_observation_inconclusive')){$code='unexpected_failed'};Write-Output (Out 'blocked' $code $false $null $null $null $null $false);exit 1 }
```
