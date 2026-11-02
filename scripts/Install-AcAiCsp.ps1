#Requires -Version 5.1
<#
.SYNOPSIS
    Installe Custom Shaders Patch (CSP) dans le clone AI d'Assetto Corsa.

.DESCRIPTION
    Les workers tournent sur le clone AI (AssettoCorsa-AI\assettocorsa), PAS sur
    l'install Steam. Content Manager installe CSP dans Steam uniquement, donc le
    clone AI n'a jamais recu `dwrite.dll` (le loader CSP). Sans lui,
    `ac.ext_resetCar()` est indisponible et la voiture n'est jamais teleportee au
    Start a la fin d'un episode (episode_done).

    Ce script copie CSP (dwrite.dll + dossier extension) depuis une SOURCE vers le
    clone AI, avec sauvegarde horodatee de l'etat actuel et verification finale.

    SOURCE possible :
      - L'install Steam d'AC apres y avoir installe CSP via Content Manager (defaut).
      - Un dossier ou une archive .zip CSP extraite (parametre -Source).

.PARAMETER Source
    Dossier (ou .zip) contenant `dwrite.dll` et `extension`. Defaut : install Steam AC.

.PARAMETER RepoRoot
    Racine du depot. Defaut : detecte depuis l'emplacement du script.

.PARAMETER Force
    Ecrase les fichiers CSP existants dans le clone AI sans demander.

.PARAMETER DryRun
    Affiche ce qui serait fait sans rien copier.

.EXAMPLE
    # 1) Installer CSP dans Steam via Content Manager, puis :
    powershell -ExecutionPolicy Bypass -File .\scripts\Install-AcAiCsp.ps1

.EXAMPLE
    # Depuis une archive CSP 0.2.1 telechargee :
    powershell -ExecutionPolicy Bypass -File .\scripts\Install-AcAiCsp.ps1 -Source "C:\Downloads\csp-0.2.1"
#>
[CmdletBinding()]
param(
    [string]$Source,
    [string]$RepoRoot,
    [switch]$Force,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step([string]$Message) { Write-Host "[CSP] $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "[OK]  $Message" -ForegroundColor Green }
function Write-Warn2([string]$Message) { Write-Host "[!]   $Message" -ForegroundColor Yellow }

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'Get-AcLocalPaths.ps1')
$paths = Get-AcLocalPaths -RepoRoot $RepoRoot

$aiRoot = $paths.AiInstallPath
$steamRoot = $paths.SteamInstallPath

Write-Step "Clone AI   : $aiRoot"
Write-Step "Steam AC   : $steamRoot"

if (-not (Test-Path -LiteralPath (Join-Path $aiRoot 'acs.exe'))) {
    throw "Clone AI invalide : acs.exe introuvable dans '$aiRoot'."
}

# --- Resoudre la SOURCE CSP -------------------------------------------------
$tempExtract = $null
if (-not $Source) {
    $Source = $steamRoot
    Write-Step "Source CSP : Steam AC (defaut)"
} else {
    if ($Source.ToLowerInvariant().EndsWith('.zip')) {
        if (-not (Test-Path -LiteralPath $Source)) { throw "Archive CSP introuvable : '$Source'." }
        $tempExtract = Join-Path ([System.IO.Path]::GetTempPath()) ("csp_extract_" + [guid]::NewGuid().ToString('N'))
        Write-Step "Extraction de l'archive CSP -> $tempExtract"
        if (-not $DryRun) {
            Expand-Archive -LiteralPath $Source -DestinationPath $tempExtract -Force
        }
        $Source = $tempExtract
    }
    Write-Step "Source CSP : $Source"
}

# La source peut pointer sur la racine AC, ou directement sur un dossier qui
# contient dwrite.dll + extension. On cherche dwrite.dll a la racine puis 1 niveau.
function Find-CspSourceRoot([string]$root) {
    if (Test-Path -LiteralPath (Join-Path $root 'dwrite.dll')) { return $root }
    $hit = Get-ChildItem -LiteralPath $root -Filter 'dwrite.dll' -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($hit) { return (Split-Path -Parent $hit.FullName) }
    return $null
}

$srcRoot = $null
if (Test-Path -LiteralPath $Source) {
    $srcRoot = Find-CspSourceRoot -root $Source
}

if (-not $srcRoot) {
    Write-Warn2 "CSP introuvable dans la source : '$Source'."
    Write-Host ''
    Write-Host 'CSP (dwrite.dll) n''est present nulle part sur ce systeme. Pour l''obtenir :' -ForegroundColor Yellow
    Write-Host '  Option A (recommandee) :' -ForegroundColor Yellow
    Write-Host '    1. Installe Content Manager : https://acstuff.ru/app/'
    Write-Host '    2. Content Manager > Settings > Custom Shaders Patch > Install (version 0.2.1)'
    Write-Host '       -> cela installe CSP dans ton AC Steam.'
    Write-Host '    3. Relance CE script SANS -Source (il copiera depuis Steam vers le clone AI).'
    Write-Host '  Option B (archive directe) :' -ForegroundColor Yellow
    Write-Host '    1. Telecharge CSP 0.2.1 : https://acstuff.club/patch/?info=0.2.1'
    Write-Host '    2. Relance : .\scripts\Install-AcAiCsp.ps1 -Source "<dossier ou .zip extrait>"'
    throw "Source CSP invalide : dwrite.dll introuvable."
}

$srcDwrite = Join-Path $srcRoot 'dwrite.dll'
$srcExtension = Join-Path $srcRoot 'extension'
Write-Ok "CSP detecte : $srcRoot"
if (-not (Test-Path -LiteralPath $srcExtension)) {
    Write-Warn2 "Dossier 'extension' absent a cote de dwrite.dll (continuera avec dwrite.dll seul)."
}

# --- Sauvegarde de l'etat actuel du clone AI --------------------------------
$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$backupDir = Join-Path $paths.RepoRoot ("ac_backups\csp_ai_install_" + $timestamp)
$dstDwrite = Join-Path $aiRoot 'dwrite.dll'
$dstExtension = Join-Path $aiRoot 'extension'

$existing = @()
if (Test-Path -LiteralPath $dstDwrite) { $existing += $dstDwrite }
if (Test-Path -LiteralPath $dstExtension) { $existing += $dstExtension }

if ($existing.Count -gt 0) {
    Write-Step "Sauvegarde de l'etat CSP existant -> $backupDir"
    if (-not $DryRun) {
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        foreach ($p in $existing) {
            $leaf = Split-Path -Leaf $p
            Copy-Item -LiteralPath $p -Destination (Join-Path $backupDir $leaf) -Recurse -Force
        }
    }
    if (-not $Force -and -not $DryRun) {
        $resp = Read-Host "CSP semble deja present dans le clone AI. Ecraser ? (o/N)"
        if ($resp -notin @('o', 'O', 'y', 'Y')) { throw 'Annule par l''utilisateur.' }
    }
}

# --- Copie dwrite.dll + extension vers le clone AI --------------------------
Write-Step "Copie dwrite.dll -> $dstDwrite"
if (-not $DryRun) {
    Copy-Item -LiteralPath $srcDwrite -Destination $dstDwrite -Force
}

if (Test-Path -LiteralPath $srcExtension) {
    Write-Step "Copie extension\ -> $dstExtension"
    if (-not $DryRun) {
        Copy-Item -LiteralPath $srcExtension -Destination $aiRoot -Recurse -Force
    }
}

# --- Nettoyage temporaire ---------------------------------------------------
if ($tempExtract -and (Test-Path -LiteralPath $tempExtract) -and -not $DryRun) {
    Remove-Item -LiteralPath $tempExtract -Recurse -Force -ErrorAction SilentlyContinue
}

# --- Verification -----------------------------------------------------------
Write-Host ''
if ($DryRun) {
    Write-Warn2 'DryRun : aucune copie effectuee.'
    return
}

$okDwrite = Test-Path -LiteralPath $dstDwrite
$okExtension = Test-Path -LiteralPath $dstExtension
if ($okDwrite) { Write-Ok "dwrite.dll present dans le clone AI." } else { Write-Warn2 "dwrite.dll TOUJOURS absent." }
if ($okExtension) { Write-Ok "extension\ present dans le clone AI." } else { Write-Warn2 "extension\ absent." }

Write-Host ''
Write-Host '=== Etapes suivantes ===' -ForegroundColor Cyan
Write-Host '  1. Verifie que CSP est ACTIVE :'
Write-Host '       Documents\Assetto Corsa\cfg  -> custom_shaders_patch.ini [BASIC] ENABLED=1'
Write-Host '     (Content Manager : Settings > Custom Shaders Patch > case "Active" cochee.)'
Write-Host '  2. emergency dans le C4D pour fermer toutes les instances AC, puis relance scale-test.'
Write-Host '  3. Au prochain episode_done, les logs AC ne doivent PLUS afficher'
Write-Host '       "ext_resetCar unavailable (CSP disabled)" ; la voiture sera teleportee au Start.'
if (-not $okDwrite) {
    throw 'Installation CSP incomplete : dwrite.dll absent apres copie.'
}
Write-Ok 'CSP installe dans le clone AI.'
