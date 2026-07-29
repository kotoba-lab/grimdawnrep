[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$VaultRemoteUrl,
    [Parameter(Mandatory = $true)]
    [switch]$CloudDisabledConfirmed,
    [ValidatePattern('^[A-Za-z0-9._-]{1,128}$')]
    [string]$MachineId = 'desktop-a',
    [switch]$ApplySetup,
    [switch]$ApplyEnroll
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Sentinel([string]$Status, [string]$Code) {
    # Intentionally contains neither paths, remote URLs, save names, nor hashes.
    [ordered]@{ sentinel = 'TERMINAL_A_HANDOFF'; status = $Status; code = $Code; machine_id = $MachineId } |
        ConvertTo-Json -Compress
}

function Stop-Safely([string]$Code) {
    Write-Output (Write-Sentinel 'blocked' $Code)
    exit 1
}

function Invoke-Quiet([string]$File, [string[]]$Arguments) {
    & $File @Arguments *> $null
    if ($LASTEXITCODE -ne 0) { throw "external_command_failed" }
}

function Invoke-Json([string]$File, [string[]]$Arguments) {
    $output = & $File @Arguments 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'sync_command_failed' }
    try { return (($output -join [Environment]::NewLine) | ConvertFrom-Json -ErrorAction Stop) }
    catch { throw 'sync_json_invalid' }
}

function Test-VaultRemoteUrl([string]$Value) {
    # HTTPS userinfo is a credential-bearing URL.  SSH's conventional git@host
    # selector is allowed, but passwords/tokens in either form are rejected.
    if ($Value -match '^[a-zA-Z][a-zA-Z0-9+.-]*://[^/@]+@' -or
        $Value -match '://[^/]*:[^/]*@' -or
        $Value -match '(?i)(token|password|oauth|x-access-token)=') { return $false }
    return $Value -match '^(https://[^/]+/.+|ssh://git@[^/]+/.+|git@[^:]+:.+)$'
}

function Get-GameInstall {
    $steamRoots = @(${env:ProgramFiles(x86)}, $env:ProgramFiles) | Where-Object { $_ } |
        ForEach-Object { Join-Path $_ 'Steam' }
    $libraries = [System.Collections.Generic.List[string]]::new()
    foreach ($steam in $steamRoots) {
        [void]$libraries.Add($steam)
        $vdf = Join-Path $steam 'steamapps\libraryfolders.vdf'
        if (Test-Path -LiteralPath $vdf -PathType Leaf) {
            foreach ($line in [System.IO.File]::ReadLines($vdf)) {
                if ($line -match '^\s*"path"\s*"(.+)"\s*$') {
                    [void]$libraries.Add(($Matches[1] -replace '\\\\', '\\'))
                }
            }
        }
    }
    foreach ($library in ($libraries | Select-Object -Unique)) {
        $candidate = Join-Path $library 'steamapps\common\Grim Dawn'
        if ((Test-Path -LiteralPath (Join-Path $candidate 'Grim Dawn.exe') -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $candidate 'DPYes.exe') -PathType Leaf)) { return $candidate }
    }
    throw 'game_or_dpyes_not_found'
}

function Assert-SourceCurrent([string]$Source) {
    Invoke-Quiet git @('-C', $Source, 'rev-parse', '--is-inside-work-tree')
    $dirty = (& git -C $Source status --porcelain)
    if ($LASTEXITCODE -ne 0 -or $dirty) { throw 'source_not_clean' }
    $remote = (& git -C $Source remote get-url origin)
    if ($LASTEXITCODE -ne 0 -or -not $remote) { throw 'source_origin_missing' }
    $head = (& git -C $Source rev-parse HEAD).Trim()
    $upstream = (& git -C $Source rev-parse 'origin/master').Trim()
    if ($LASTEXITCODE -ne 0 -or $head -ne $upstream) { throw 'source_not_current' }
}

function Assert-ExistingConfig([string]$Path, [System.Collections.IDictionary]$Expected) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try { $actual = Get-Content -LiteralPath $Path -Raw -Encoding utf8 | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'existing_config_invalid' }
    foreach ($key in $Expected.Keys) {
        $want = $Expected[$key]
        $got = $actual.$key
        if ($want -is [array]) {
            if (($got -join "`n") -ne ($want -join "`n")) { throw 'existing_config_mismatch' }
        } elseif ([string]$got -ne [string]$want) { throw 'existing_config_mismatch' }
    }
    return $true
}

function Assert-Doctor([object]$Doctor) {
    if ($Doctor.command -ne 'doctor' -or -not $Doctor.read_only -or -not $Doctor.checks.config.ok) { throw 'doctor_invalid' }
    if (-not $Doctor.checks.processes.complete -or $Doctor.checks.processes.status -ne 'clear') { throw 'doctor_processes_not_clear' }
    $vault = $Doctor.checks.vault
    if ($vault.git -ne 'available' -or $vault.relation -ne 'equal' -or $vault.active_lock) { throw 'doctor_vault_not_ready' }
    $launcher = $Doctor.checks.launcher
    if (-not $launcher.launcher_path.exists -or $launcher.launcher_path.type -ne 'file') { throw 'doctor_launcher_missing' }
    if (-not ($launcher.game_executables | Where-Object { $_.exists -and $_.type -eq 'file' })) { throw 'doctor_game_missing' }
}

function Assert-ProcessesStopped {
    $running = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -in @('Grim Dawn', 'DPYes') }
    if ($running) { throw 'game_or_dpyes_running' }
}

function Assert-Vault([string]$Vault) {
    if (-not (Test-Path -LiteralPath $Vault -PathType Container)) { return $false }
    Invoke-Quiet git @('-C', $Vault, 'rev-parse', '--is-inside-work-tree')
    $dirty = (& git -C $Vault status --porcelain)
    $origin = (& git -C $Vault remote get-url origin).Trim()
    if ($LASTEXITCODE -ne 0 -or $dirty -or $origin -ne $VaultRemoteUrl) { throw 'vault_existing_not_clean_or_origin_mismatch' }
    return $true
}

try {
    if (-not $CloudDisabledConfirmed) { throw 'cloud_disabled_confirmation_required' }
    if ($MachineId -eq 'melofla') { throw 'machine_id_collision' }
    if (-not (Test-VaultRemoteUrl $VaultRemoteUrl)) { throw 'vault_remote_url_invalid_or_credential_bearing' }
    if ($ApplyEnroll) { $ApplySetup = $true }

    $source = Split-Path -Parent $PSScriptRoot
    Assert-SourceCurrent $source
    Assert-ProcessesStopped
    $gameInstall = Get-GameInstall
    $saveRoot = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'My Games\Grim Dawn\save'
    $localRoot = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSync'
    $configPath = Join-Path $localRoot 'config.local.json'
    $vaultPath = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveVault'
    $toolRoot = Join-Path $env:LOCALAPPDATA 'GrimDawnSaveSyncTool'
    $venvPath = Join-Path $toolRoot '.venv'
    $expectedConfig = [ordered]@{
        schema_version = '1.0.0'; machine_id = $MachineId; save_root = $saveRoot; vault_repo = $vaultPath
        remote = 'origin'; branch = 'main'; game_install = $gameInstall; launcher_mode = 'dpyes'
        launcher_path = (Join-Path $gameInstall 'DPYes.exe'); game_process_names = @('Grim Dawn.exe')
        launch_timeout_seconds = 120; stable_window_seconds = 3; stable_scan_retries = 3; offline_policy = 'deny'
    }

    # Default mode is diagnostic-only: no fetch, clone, venv, config, or save mutation.
    if (-not $ApplySetup) {
        if (Test-Path -LiteralPath $configPath -PathType Leaf) { [void](Assert-ExistingConfig $configPath $expectedConfig) }
        if (Test-Path -LiteralPath $vaultPath -PathType Container) { [void](Assert-Vault $vaultPath) }
        Write-Output (Write-Sentinel 'ready_for_setup' 'diagnostics_passed')
        exit 0
    }

    $py = Get-Command py -ErrorAction Stop
    # Let the Windows launcher choose the installed Python 3.  Do not pin a
    # minor version: terminal A may legitimately have only 3.12 or later.
    & $py.Source '-3' '-c' 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' *> $null
    if ($LASTEXITCODE -ne 0) { throw 'python_3_11_or_later_required' }
    $env:GIT_TERMINAL_PROMPT = '0'
    Invoke-Quiet git @('-C', $source, 'fetch', 'origin')
    Assert-SourceCurrent $source
    Assert-ProcessesStopped

    if (-not (Test-Path -LiteralPath $venvPath -PathType Container)) {
        Invoke-Quiet $py.Source @('-3', '-m', 'venv', $venvPath)
    }
    $python = Join-Path $venvPath 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'venv_invalid' }
    Invoke-Quiet $python @('-m', 'pip', 'install', '--no-deps', '--no-build-isolation', '-e', $source)

    if (-not (Assert-Vault $vaultPath)) {
        Invoke-Quiet git @('clone', $VaultRemoteUrl, $vaultPath)
        [void](Assert-Vault $vaultPath)
    }

    if (-not (Test-Path -LiteralPath $configPath)) {
        New-Item -ItemType Directory -Force -Path $localRoot | Out-Null
        $json = $expectedConfig | ConvertTo-Json -Depth 3
        [System.IO.File]::WriteAllText($configPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    } else {
        [void](Assert-ExistingConfig $configPath $expectedConfig)
    }

    # Doctor is always read-only and is required before either enrollment mode.
    $doctor = Invoke-Json $python @('-m', 'grim_dawn_sync', '--config', $configPath, '--json', 'doctor')
    Assert-Doctor $doctor
    if (-not $ApplyEnroll) {
        Write-Output (Write-Sentinel 'setup_complete' 'doctor_passed')
        exit 0
    }

    # enroll dry-run precedes the only save-writing operation in this handoff.
    Invoke-Quiet $python @('-m', 'grim_dawn_sync', '--config', $configPath, '--json', 'enroll')
    Assert-ProcessesStopped
    Invoke-Quiet $python @('-m', 'grim_dawn_sync', '--config', $configPath, '--json', 'enroll', '--apply')
    Write-Output (Write-Sentinel 'enrolled' 'enroll_apply_passed')
    exit 0
}
catch {
    $code = if ($_.Exception.Message -match '^[a-z0-9_]+$') { $_.Exception.Message } else { 'handoff_failed' }
    Write-Output (Write-Sentinel 'blocked' $code)
    exit 1
}
