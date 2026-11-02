#Requires -Version 5.1
<#
.SYNOPSIS
    Active le profil AI et lance Content Manager depuis l'instance dediee (copie AI uniquement).
#>
[CmdletBinding()]
param(
    [switch]$SkipProfileSwitch,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
} catch {
}

function Get-NormalizedPath([string]$Path) {
    if (-not $Path) { return $null }
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Write-CmAiLaunchLog {
    param([hashtable]$Entry, [string]$LogDir)
    if (-not (Test-Path -LiteralPath $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }
    $logFile = Join-Path $LogDir 'cm_ai_launch.log'
    $parts = @()
    foreach ($kv in $Entry.GetEnumerator()) {
        $parts += [string]$kv.Key + '=' + [string]$kv.Value
    }
    $line = $parts -join ' | '
    Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
}

function Show-VisibleLaunchError {
    param([string]$Message)
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            $Message,
            'Assetto Corsa AI',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    } catch {
        Start-Process -FilePath 'powershell.exe' -ArgumentList @(
            '-NoProfile', '-Command',
            "Write-Host '$($Message -replace "'", "''")' -ForegroundColor Red; Read-Host 'Appuyez sur Entree pour fermer'"
        ) -Wait | Out-Null
    }
}

function Get-ContentManagerAiExecutable {
    param([string]$InstallDir)

    $candidates = @(
        'Content Manager.exe',
        'ContentManager.exe'
    )
    foreach ($name in $candidates) {
        $path = Join-Path $InstallDir $name
        if (Test-Path -LiteralPath $path) {
            return [pscustomobject]@{
                FileName = $name
                Path     = (Get-NormalizedPath $path)
            }
        }
    }
    return $null
}

function Test-ContentManagerAiUpdatePresent {
    param([string]$InstallDir)
    return Test-Path -LiteralPath (Join-Path $InstallDir 'Content Manager.update.exe')
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'Get-AcLocalPaths.ps1')
$paths = Get-AcLocalPaths

$aiRoot = $paths.AiInstallPath
$steamRoot = $paths.SteamInstallPath
$cmDir = $paths.ContentManagerAiPath
$cmInstall = Get-ContentManagerAiExecutable -InstallDir $cmDir
$cmExe = if ($cmInstall) { $cmInstall.Path } else { $null }
$cmUpdatePresent = Test-ContentManagerAiUpdatePresent -InstallDir $cmDir
$marker = Join-Path $aiRoot 'AI_INSTANCE.txt'
$logDir = Join-Path $paths.RepoRoot 'ac_launch_logs'

$logBase = @{
    Timestamp       = (Get-Date).ToUniversalTime().ToString('o')
    AiRoot          = $aiRoot
    SteamRoot       = $steamRoot
    ContentManager  = $(if ($cmExe) { $cmExe } else { (Join-Path $cmDir 'Content Manager.exe') })
    Success         = 'false'
    Detail          = ''
}

if (-not (Test-Path -LiteralPath $marker)) {
    $msg = "Copie AI invalide (AI_INSTANCE.txt absent) : $aiRoot"
    Write-CmAiLaunchLog -Entry (@{ Timestamp = $logBase.Timestamp; Success = 'false'; Detail = $msg }) -LogDir $logDir
    Show-VisibleLaunchError -Message $msg
    exit 1
}

if ($aiRoot -eq $steamRoot) {
    $msg = 'Refus : chemin AI identique a Steam.'
    Write-CmAiLaunchLog -Entry (@{ Timestamp = $logBase.Timestamp; Success = 'false'; Detail = $msg }) -LogDir $logDir
    Show-VisibleLaunchError -Message $msg
    exit 1
}

if ($aiRoot.StartsWith($steamRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    $msg = 'Refus : copie AI a l''interieur de l''installation Steam.'
    Write-CmAiLaunchLog -Entry (@{ Timestamp = $logBase.Timestamp; Success = 'false'; Detail = $msg }) -LogDir $logDir
    Show-VisibleLaunchError -Message $msg
    exit 1
}

$steamRunning = $null -ne (Get-Process -Name 'steam' -ErrorAction SilentlyContinue)
if (-not $DryRun -and -not $steamRunning) {
    $msg = 'Steam doit etre ouvert avant de lancer Content Manager AI (authentification DRM).'
    Write-CmAiLaunchLog -Entry (@{ Timestamp = $logBase.Timestamp; Success = 'false'; Detail = $msg }) -LogDir $logDir
    Show-VisibleLaunchError -Message $msg
    exit 1
}

if ($DryRun) {
    if (-not $SkipProfileSwitch) {
        Write-Host '[DRY-RUN] Switch-AssettoCorsaProfile.ps1 -Profile AI'
    }
    if ($cmInstall) {
        Write-Host "[DRY-RUN] Start-Process -FilePath '$($cmInstall.Path)' -WorkingDirectory '$cmDir'"
        Write-Host "[DRY-RUN] Executable detecte : $($cmInstall.FileName)"
    } else {
        Write-Host "[DRY-RUN] Executable detecte : aucun"
    }
    Write-Host "[DRY-RUN] Racine AC attendue dans CM : $aiRoot"
    Write-Host "[DRY-RUN] Content Manager.update.exe present : $cmUpdatePresent"
    exit 0
}

if (-not $cmExe) {
    $msg = @"
Configuration Content Manager AI incomplete.

1. Telechargez Content Manager (version Lite) depuis la source officielle :
   https://acstuff.club/app/

2. Extrayez le fichier officiel dans :
   $cmDir

   Nom attendu (prioritaire) : Content Manager.exe
   Variante acceptee : ContentManager.exe

3. Au premier lancement, choisissez comme racine Assetto Corsa :
   $aiRoot

Ne selectionnez jamais :
   $steamRoot
"@
    Write-CmAiLaunchLog -Entry (@{ Timestamp = $logBase.Timestamp; Success = 'false'; Detail = 'Content Manager.exe absent' }) -LogDir $logDir
    Show-VisibleLaunchError -Message $msg
    exit 1
}

try {
    if (-not $SkipProfileSwitch) {
        & (Join-Path $scriptDir 'Switch-AssettoCorsaProfile.ps1') -Profile AI -AcDocumentsPath $paths.AcDocumentsPath -RepoRoot $paths.RepoRoot
    }

    Start-Process -FilePath $cmExe -WorkingDirectory $cmDir -ErrorAction Stop | Out-Null

    $logBase.Success = 'true'
    $logBase.Detail = 'Content Manager AI lance (racine AC attendue : copie AI)'
    Write-CmAiLaunchLog -Entry $logBase -LogDir $logDir
}
catch {
    $logBase.Detail = $_.Exception.Message
    Write-CmAiLaunchLog -Entry $logBase -LogDir $logDir
    Show-VisibleLaunchError -Message $_.Exception.Message
    exit 1
}
