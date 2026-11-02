#Requires -Version 5.1
<#
.SYNOPSIS
    Bascule le profil Documents Assetto Corsa entre Clean et AI.
    Ne supprime jamais le dossier Documents complet.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Clean', 'AI')]
    [string]$Profile,

    [string]$AcDocumentsPath,
    [string]$ProfilesRoot,
    [string]$RepoRoot,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Info([string]$Message) { Write-Host "[INFO] $Message" }
function Write-Warn([string]$Message) { Write-Host "[WARN] $Message" -ForegroundColor Yellow }

function Get-ProjectRoot {
    return (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
}

function Find-AcDocumentsPath {
    param([string]$Preferred)
    if ($Preferred) {
        if (Test-Path -LiteralPath $Preferred) { return (Resolve-Path -LiteralPath $Preferred).Path }
        throw "AcDocumentsPath introuvable : $Preferred"
    }
    foreach ($c in @(
        (Join-Path $env:USERPROFILE 'OneDrive\Documents\Assetto Corsa')
        (Join-Path $env:USERPROFILE 'Documents\Assetto Corsa')
        (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Assetto Corsa')
    )) {
        if (Test-Path -LiteralPath $c) { return (Resolve-Path -LiteralPath $c).Path }
    }
    throw "Dossier Documents Assetto Corsa introuvable."
}

function Test-GameProcessesClosed {
    $names = @('AssettoCorsa', 'acs', 'acs_x86', 'Content Manager')
    $running = @()
    foreach ($n in $names) {
        if (Get-Process -Name $n -ErrorAction SilentlyContinue) { $running += $n }
    }
    if ($running.Count -gt 0) {
        throw "Fermez le jeu et Content Manager avant de changer de profil : $($running -join ', ')"
    }
}

function Get-ManagedPaths {
    param([string]$ProfilesRootPath)
    $manifestPath = Join-Path $ProfilesRootPath 'profile_manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        throw "Manifeste profils introuvable : $manifestPath"
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    return @($manifest.managedRelativePaths)
}

function Get-ActiveProfileStatePath([string]$ProjectRoot) {
    return Join-Path $ProjectRoot 'ac_profiles\active_profile.json'
}

function Read-ActiveProfile([string]$ProjectRoot) {
    $statePath = Get-ActiveProfileStatePath -ProjectRoot $ProjectRoot
    if (-not (Test-Path -LiteralPath $statePath)) { return 'Unknown' }
    return (Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json).ActiveProfile
}

# --- Main ---
Test-GameProcessesClosed

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'Get-AcLocalPaths.ps1')
$pathCfg = Get-AcLocalPaths -RepoRoot $RepoRoot

$projectRoot = if ($RepoRoot) { (Resolve-Path -LiteralPath $RepoRoot).Path } else { $pathCfg.RepoRoot }
$profilesRoot = if ($ProfilesRoot) { (Resolve-Path -LiteralPath $ProfilesRoot).Path } else { (Join-Path $projectRoot 'ac_profiles') }
$docs = if ($AcDocumentsPath) {
    Find-AcDocumentsPath -Preferred $AcDocumentsPath
} else {
    $pathCfg.AcDocumentsPath
}
$profileDir = Join-Path $profilesRoot ($Profile.ToLowerInvariant())
if (-not (Test-Path -LiteralPath $profileDir)) {
    throw "Profil '$Profile' introuvable : $profileDir"
}

$managed = Get-ManagedPaths -ProfilesRootPath $profilesRoot
$current = Read-ActiveProfile -ProjectRoot $projectRoot
Write-Info "Profil actif (état projet) : $current"
Write-Info "Profil cible           : $Profile"
Write-Info "Documents AC           : $docs"

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$switchBackup = Join-Path $profilesRoot ("switches\before_{0}_{1}" -f $Profile, $stamp)
$actions = @()

foreach ($rel in $managed) {
    $relWin = $rel -replace '/', '\'
    $srcProfile = Join-Path $profileDir $relWin
    $destLive = Join-Path $docs $relWin
    if (-not (Test-Path -LiteralPath $srcProfile)) {
        Write-Warn "Fichier absent dans le profil $Profile : $rel"
        continue
    }
    $actions += [ordered]@{
        From = $srcProfile
        To   = $destLive
        Rel  = $rel
    }
}

if ($DryRun) {
    Write-Info "[DRY-RUN] Sauvegarde prévue de la config active vers : $switchBackup"
    foreach ($rel in $managed) {
        $live = Join-Path $docs ($rel -replace '/', '\')
        if (Test-Path -LiteralPath $live) {
            Write-Host "  backup: $live"
        }
    }
    foreach ($a in $actions) {
        Write-Host "  apply [$Profile]: $($a.From) -> $($a.To)"
    }
    Write-Info "[DRY-RUN] Aucun fichier modifié."
    exit 0
}

New-Item -ItemType Directory -Path $switchBackup -Force | Out-Null
foreach ($rel in $managed) {
    $relWin = $rel -replace '/', '\'
    $live = Join-Path $docs $relWin
    if (Test-Path -LiteralPath $live) {
        $bakDest = Join-Path $switchBackup $relWin
        $bakParent = [System.IO.Path]::GetDirectoryName($bakDest)
        if (-not (Test-Path -LiteralPath $bakParent)) {
            New-Item -ItemType Directory -Path $bakParent -Force | Out-Null
        }
        Copy-Item -LiteralPath $live -Destination $bakDest -Force
    }
}

foreach ($a in $actions) {
    $destParent = [System.IO.Path]::GetDirectoryName($a.To)
    if (-not (Test-Path -LiteralPath $destParent)) {
        New-Item -ItemType Directory -Path $destParent -Force | Out-Null
    }
    Copy-Item -LiteralPath $a.From -Destination $a.To -Force
    Write-Host "  Appliqué : $($a.Rel)"
}

$state = [ordered]@{
    ActiveProfile   = $Profile
    SwitchedAtUtc   = (Get-Date).ToUniversalTime().ToString('o')
    DocumentsPath   = $docs
    SwitchBackupDir = $switchBackup
    FilesApplied    = $actions.Count
}
$statePath = Get-ActiveProfileStatePath -ProjectRoot $projectRoot
$state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8
Write-Info "Profil actif enregistré : $statePath"
