#Requires -Version 5.1
<#
.SYNOPSIS
    Duplique l'installation Assetto Corsa vers un emplacement AI séparé (Phase C0).
    Ne modifie jamais la source. N'utilise jamais robocopy /MIR.
#>
[CmdletBinding()]
param(
    [string]$SourcePath,
    [string]$DestinationPath,
    [string]$RepoRoot,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Info([string]$Message) { Write-Host "[INFO] $Message" }
function Write-Warn([string]$Message) { Write-Host "[WARN] $Message" -ForegroundColor Yellow }

function Get-NormalizedPath([string]$Path) {
    if (-not $Path) { return $null }
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Get-ProjectRoot {
    param([string]$StartPath = $PSScriptRoot)
    return (Resolve-Path -LiteralPath (Join-Path $StartPath '..')).Path
}

function Find-AssettoCorsaInstallPath {
    param([string]$PreferredPath)
    if ($PreferredPath) {
        $exe = Join-Path $PreferredPath 'AssettoCorsa.exe'
        if (Test-Path -LiteralPath $exe) { return (Get-NormalizedPath $PreferredPath) }
        throw "SourcePath invalide : AssettoCorsa.exe introuvable dans '$PreferredPath'"
    }
    $reg = Get-ItemProperty -Path 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue
    if ($reg -and $reg.SteamPath) {
        $vdf = Join-Path $reg.SteamPath 'steamapps\libraryfolders.vdf'
        $libraries = @($reg.SteamPath)
        if (Test-Path -LiteralPath $vdf) {
            $content = Get-Content -LiteralPath $vdf -Raw
            foreach ($m in [regex]::Matches($content, '"path"\s+"([^"]+)"')) {
                $p = $m.Groups[1].Value -replace '\\\\', '\'
                if (Test-Path -LiteralPath $p) { $libraries += $p }
            }
        }
        foreach ($lib in ($libraries | Select-Object -Unique)) {
            $candidate = Join-Path $lib 'steamapps\common\assettocorsa'
            if (Test-Path -LiteralPath (Join-Path $candidate 'AssettoCorsa.exe')) {
                return (Get-NormalizedPath $candidate)
            }
        }
    }
    $fallback = Join-Path ${env:ProgramFiles(x86)} 'Steam\steamapps\common\assettocorsa'
    if (Test-Path -LiteralPath (Join-Path $fallback 'AssettoCorsa.exe')) {
        return (Get-NormalizedPath $fallback)
    }
    throw "Installation Steam Assetto Corsa introuvable."
}

function Test-ClonePathsAllowed {
    param(
        [string]$Source,
        [string]$Destination,
        [string]$ProjectRoot
    )

    $src = Get-NormalizedPath $Source
    $dst = Get-NormalizedPath $Destination
    $proj = Get-NormalizedPath $ProjectRoot

    if ($src -eq $dst) { throw "Destination identique à la source." }
    if ($dst.StartsWith($src + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Destination à l'intérieur de la source."
    }
    if ($src.StartsWith($dst + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "La source est à l'intérieur de la destination."
    }
    if ($dst.StartsWith($proj + '\', [StringComparison]::OrdinalIgnoreCase)) {
        $allowedAiRoot = Get-NormalizedPath (Join-Path $proj 'AssettoCorsa-AI\assettocorsa')
        $allowedAiParent = Get-NormalizedPath (Join-Path $proj 'AssettoCorsa-AI')
        $ok = ($dst -eq $allowedAiRoot) -or ($dst.StartsWith($allowedAiParent + '\', [StringComparison]::OrdinalIgnoreCase))
        if (-not $ok) {
            throw "Destination interdite dans le dépôt hors AssettoCorsa-AI : $dst"
        }
    }
    if ($dst -match '\\steamapps\\common\\assettocorsa(\\|$)') {
        throw "Destination interdite dans steamapps\common\assettocorsa."
    }
    return @{ Source = $src; Destination = $dst }
}

function Get-DirectoryStats {
    param([string]$Root)
    $files = Get-ChildItem -LiteralPath $Root -Recurse -File -Force -ErrorAction SilentlyContinue
    $dirs = Get-ChildItem -LiteralPath $Root -Recurse -Directory -Force -ErrorAction SilentlyContinue
    $size = ($files | Measure-Object -Property Length -Sum).Sum
    if (-not $size) { $size = 0 }
    return @{
        FileCount = $files.Count
        DirCount  = $dirs.Count
        SizeBytes = [int64]$size
    }
}

function Get-DriveFreeBytes([string]$Path) {
    $root = [System.IO.Path]::GetPathRoot($Path)
    $disk = Get-CimInstance Win32_LogicalDisk -Filter ("DeviceID='{0}'" -f $root.TrimEnd('\'))
    return [int64]$disk.FreeSpace
}

function Test-ProcessesClosed {
    $names = @('AssettoCorsa', 'acs', 'acs_x86', 'Content Manager', 'steam')
    $running = @()
    foreach ($n in $names) {
        $p = Get-Process -Name $n -ErrorAction SilentlyContinue
        if ($p) { $running += $n }
    }
    if ($running.Count -gt 0) {
        throw "Processus encore actifs : $($running -join ', '). Fermez-les avant la copie."
    }
}

function Invoke-RobocopyClone {
    param(
        [string]$Source,
        [string]$Destination,
        [string]$LogFile,
        [switch]$IsDryRun
    )

    if (-not $IsDryRun) {
        $parent = [System.IO.Path]::GetDirectoryName($Destination)
        if (-not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        if (-not (Test-Path -LiteralPath $Destination)) {
            New-Item -ItemType Directory -Path $Destination -Force | Out-Null
        }
    }

    $roboArgs = @(
        $Source,
        $Destination,
        '/E',
        '/COPY:DAT',
        '/R:2',
        '/W:2',
        '/XJ',
        '/XJD',
        '/BYTES',
        '/NP',
        '/NFL',
        '/NDL',
        ('/LOG:{0}' -f $LogFile)
    )
    if ($IsDryRun) { $roboArgs += '/L' }

    & robocopy.exe @roboArgs | Out-Null
    $exitCode = $LASTEXITCODE
    if ($exitCode -gt 7) {
        throw "Robocopy a échoué avec le code $exitCode. Voir $LogFile"
    }
    return $exitCode
}

function Get-FileSha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

function Write-AiInstanceMarker {
    param(
        [string]$AiRoot,
        [string]$CleanSource,
        [int64]$CopiedBytes,
        [string]$CreatedAtUtc
    )
    $content = @"
Assetto Corsa AI Instance
=========================
Dedicated installation for Assetto Corsa Gym / RL experiments.
DO NOT run Steam "Verify integrity" on this copy.

Clean Steam source : $CleanSource
AI install path    : $AiRoot
Created (UTC)      : $CreatedAtUtc
Copied size (bytes): $CopiedBytes

All AI plugins, vJoy configs, CSP and Gym files must be installed ONLY here.
The Steam installation must remain untouched.
"@
    $marker = Join-Path $AiRoot 'AI_INSTANCE.txt'
    Set-Content -LiteralPath $marker -Value $content -Encoding UTF8
}

# --- Main ---
Test-ProcessesClosed

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'Get-AcLocalPaths.ps1')
$pathCfg = Get-AcLocalPaths -RepoRoot $RepoRoot
$projectRoot = $pathCfg.RepoRoot
$resolvedDest = if ($DestinationPath) { $DestinationPath } else { $pathCfg.AiInstallPath }
$source = Find-AssettoCorsaInstallPath -PreferredPath $SourcePath
$paths = Test-ClonePathsAllowed -Source $source -Destination $resolvedDest -ProjectRoot $projectRoot
$source = $paths.Source
$destination = $paths.Destination

$sourceStats = Get-DirectoryStats -Root $source
$requiredBytes = [int64][math]::Ceiling($sourceStats.SizeBytes * 1.2) + 10GB
$freeBytes = Get-DriveFreeBytes -Path $destination
$remainingEstimate = $freeBytes - $sourceStats.SizeBytes

Write-Info "Taille source      : $([math]::Round($sourceStats.SizeBytes/1GB,2)) Go ($($sourceStats.FileCount) fichiers)"
Write-Info "Espace libre disque: $([math]::Round($freeBytes/1GB,2)) Go"
Write-Info "Destination prévue : $destination"
Write-Info "Espace requis est.: $([math]::Round($requiredBytes/1GB,2)) Go (copie + 20% + 10 Go futurs)"
Write-Info "Reste estimé après : $([math]::Round($remainingEstimate/1GB,2)) Go"

if ($freeBytes -lt $requiredBytes) {
    throw "Espace insuffisant sur le disque cible. Copie annulée."
}

$logDir = Join-Path $projectRoot 'ac_clone_logs'
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$logFile = Join-Path $logDir ("robocopy_{0}.log" -f $stamp)

if ($DryRun) {
    Write-Info "[DRY-RUN] Simulation robocopy vers $destination"
} else {
    Write-Info "Copie en cours (journal : $logFile)"
}

$sourceBefore = @{
    Stats = $sourceStats
    AssettoCorsaExe = Get-FileSha256 (Join-Path $source 'AssettoCorsa.exe')
    AcsExe = Get-FileSha256 (Join-Path $source 'acs.exe')
}

Invoke-RobocopyClone -Source $source -Destination $destination -LogFile $logFile -IsDryRun:$DryRun

if ($DryRun) {
    Write-Info "[DRY-RUN] Terminé. Aucun fichier copié."
    exit 0
}

$destStats = Get-DirectoryStats -Root $destination
$verification = @{
    SourceFileCount      = $sourceBefore.Stats.FileCount
    DestFileCount        = $destStats.FileCount
    SourceSizeBytes      = $sourceBefore.Stats.SizeBytes
    DestSizeBytes        = $destStats.SizeBytes
    FileCountMatch       = ($sourceBefore.Stats.FileCount -eq $destStats.FileCount)
    SizeDeltaBytes       = [math]::Abs($sourceBefore.Stats.SizeBytes - $destStats.SizeBytes)
    AssettoCorsaExeMatch = ((Get-FileSha256 (Join-Path $destination 'AssettoCorsa.exe')) -eq $sourceBefore.AssettoCorsaExe)
    AcsExeMatch          = ((Get-FileSha256 (Join-Path $destination 'acs.exe')) -eq $sourceBefore.AcsExe)
    SourceUnchanged      = ((Get-FileSha256 (Join-Path $source 'AssettoCorsa.exe')) -eq $sourceBefore.AssettoCorsaExe)
}

$steamApps = Join-Path (Get-NormalizedPath (Join-Path $source '..\..')) 'appmanifest_244210.acf'
$steamPointsToClean = Test-Path -LiteralPath $steamApps

$createdUtc = (Get-Date).ToUniversalTime().ToString('o')
Write-AiInstanceMarker -AiRoot $destination -CleanSource $source -CopiedBytes $destStats.SizeBytes -CreatedAtUtc $createdUtc

$manifestDir = Join-Path $projectRoot 'ac_clone_manifests'
New-Item -ItemType Directory -Path $manifestDir -Force | Out-Null
$manifest = [ordered]@{
    SchemaVersion        = 1
    CreatedAtUtc         = $createdUtc
    SourcePath           = $source
    DestinationPath      = $destination
    SourceFileCount      = $sourceBefore.Stats.FileCount
    DestFileCount        = $destStats.FileCount
    SourceSizeBytes      = $sourceBefore.Stats.SizeBytes
    DestSizeBytes        = $destStats.SizeBytes
    FreeBytesAfter       = (Get-DriveFreeBytes -Path $destination)
    CriticalHashes       = [ordered]@{
        SourceAssettoCorsaExe = $sourceBefore.AssettoCorsaExe
        DestAssettoCorsaExe   = (Get-FileSha256 (Join-Path $destination 'AssettoCorsa.exe'))
        SourceAcsExe          = $sourceBefore.AcsExe
        DestAcsExe            = (Get-FileSha256 (Join-Path $destination 'acs.exe'))
    }
    Verification         = $verification
    SteamManifestPresent = $steamPointsToClean
    RobocopyLog          = $logFile
}
$manifestPath = Join-Path $manifestDir ("clone_{0}.json" -f $stamp)
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Write-Info "Manifeste : $manifestPath"
Write-Info "Fichiers copiés : $($destStats.FileCount)"
Write-Info "Vérification taille delta : $($verification.SizeDeltaBytes) octets"
Write-Info "Source inchangée : $($verification.SourceUnchanged)"
if (-not $verification.FileCountMatch) {
    Write-Warn "Écart nombre de fichiers source/destination."
}

exit 0
