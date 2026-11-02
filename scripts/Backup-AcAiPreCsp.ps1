#Requires -Version 5.1
<#
.SYNOPSIS
    Sauvegarde horodatee pre-CSP de la copie AI et configs Documents concernees.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string]$BackupRoot,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-NormalizedPath([string]$Path) {
    if (-not $Path) { return $null }
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function New-InventoryEntry {
    param(
        [string]$Scope,
        [string]$SourcePath,
        [string]$Kind,
        [bool]$Present
    )
    $sizeBytes = $null
    $sha256 = $null
    $lastWrite = $null
    if ($Present -and (Test-Path -LiteralPath $SourcePath)) {
        $item = Get-Item -LiteralPath $SourcePath
        if ($item.PSIsContainer) {
            $Kind = 'directory'
        } else {
            $sizeBytes = $item.Length
            $lastWrite = $item.LastWriteTimeUtc.ToString('o')
            $sha256 = (Get-FileHash -LiteralPath $SourcePath -Algorithm SHA256).Hash
        }
    }
    return @{
        Scope            = $Scope
        SourcePath       = $SourcePath
        Kind             = $Kind
        Present          = $Present
        SizeBytes        = $sizeBytes
        Sha256           = $sha256
        LastWriteTimeUtc = $lastWrite
    }
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'Get-AcLocalPaths.ps1')
$paths = Get-AcLocalPaths -RepoRoot $RepoRoot

$aiRoot = $paths.AiInstallPath
$steamRoot = $paths.SteamInstallPath
$docsRoot = $paths.AcDocumentsPath
$projectRoot = $paths.RepoRoot
$backupRootResolved = if ($BackupRoot) {
    Get-NormalizedPath $BackupRoot
} else {
    Join-Path $projectRoot 'ac_backups'
}

$running = @('acs', 'AssettoCorsa', 'acs_x86') | ForEach-Object {
    Get-Process -Name $_ -ErrorAction SilentlyContinue
} | Where-Object { $_ }
if ($running) {
    throw "Fermez Assetto Corsa avant la sauvegarde pre-CSP. Processus actifs : $(($running | ForEach-Object { $_.ProcessName + ':' + $_.Id }) -join ', ')"
}

if (-not (Test-Path -LiteralPath (Join-Path $aiRoot 'AI_INSTANCE.txt'))) {
    throw "Copie AI invalide : AI_INSTANCE.txt absent dans $aiRoot"
}

$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$backupDir = Join-Path $backupRootResolved "pre_csp_ai_$timestamp"

$candidates = @(
    @{ Scope = 'game_ai'; Path = (Join-Path $aiRoot 'dwrite.dll') }
    @{ Scope = 'game_ai'; Path = (Join-Path $aiRoot 'extension') }
    @{ Scope = 'documents'; Path = (Join-Path $docsRoot 'cfg\custom_shaders_patch.ini') }
    @{ Scope = 'documents'; Path = (Join-Path $docsRoot 'cfg\extension') }
    @{ Scope = 'documents'; Path = (Join-Path $docsRoot 'cfg\video.ini') }
)

$seenPaths = @{}
$inventory = @()
foreach ($c in $candidates) {
    $pathKey = $c.Path.ToLowerInvariant()
    if ($seenPaths.ContainsKey($pathKey)) { continue }
    $seenPaths[$pathKey] = $true
    $present = Test-Path -LiteralPath $c.Path
    $kind = if ($present -and (Get-Item -LiteralPath $c.Path).PSIsContainer) { 'directory' } else { 'file' }
    $inventory += New-InventoryEntry -Scope $c.Scope -SourcePath $c.Path -Kind $kind -Present $present
}

$steamBaseline = @(
    (New-InventoryEntry -Scope 'steam_baseline' -SourcePath (Join-Path $steamRoot 'dwrite.dll') -Kind 'file' -Present (Test-Path -LiteralPath (Join-Path $steamRoot 'dwrite.dll')))
    (New-InventoryEntry -Scope 'steam_baseline' -SourcePath (Join-Path $steamRoot 'extension') -Kind 'directory' -Present (Test-Path -LiteralPath (Join-Path $steamRoot 'extension')))
    (New-InventoryEntry -Scope 'steam_baseline' -SourcePath (Join-Path $steamRoot 'AI_INSTANCE.txt') -Kind 'file' -Present (Test-Path -LiteralPath (Join-Path $steamRoot 'AI_INSTANCE.txt')))
)

$filesToCopy = @()
foreach ($item in $inventory) {
    if (-not $item.Present) { continue }
    $src = $item.SourcePath
    if ((Get-Item -LiteralPath $src).PSIsContainer) {
        Get-ChildItem -LiteralPath $src -Recurse -File | ForEach-Object {
            $rel = $_.FullName.Substring($src.Length).TrimStart('\')
            $filesToCopy += @{
                Scope      = $item.Scope
                SourcePath = $_.FullName
                BackupPath = ($item.Scope + '/' + (Split-Path -Leaf $src) + '/' + ($rel -replace '\\', '/'))
            }
        }
    } else {
        $filesToCopy += @{
            Scope      = $item.Scope
            SourcePath = $src
            BackupPath = ($item.Scope + '/' + (Split-Path -Leaf $src))
        }
    }
}

Write-Host "[INFO] Copie AI      : $aiRoot"
Write-Host "[INFO] Steam (ref)   : $steamRoot"
Write-Host "[INFO] Documents     : $docsRoot"
Write-Host "[INFO] Sauvegarde    : $backupDir"
Write-Host "[INFO] Fichiers CSP existants a copier : $($filesToCopy.Count)"

if ($DryRun) {
    Write-Host '[DRY-RUN] Aucune copie effectuee.'
    foreach ($f in $filesToCopy) {
        Write-Host "  $($f.SourcePath)"
    }
    exit 0
}

New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

$copiedEntries = @()
foreach ($f in $filesToCopy) {
    $dest = [System.IO.Path]::Combine($backupDir, ($f.BackupPath -replace '/', '\'))
    $destParent = Split-Path -Parent $dest
    if ($destParent -and -not (Test-Path -LiteralPath $destParent)) {
        New-Item -ItemType Directory -Path $destParent -Force | Out-Null
    }
    Copy-Item -LiteralPath $f.SourcePath -Destination $dest -Force
    $hash = (Get-FileHash -LiteralPath $f.SourcePath -Algorithm SHA256).Hash
    $info = Get-Item -LiteralPath $f.SourcePath
    $copiedEntries += @{
        Scope            = $f.Scope
        SourcePath       = $f.SourcePath
        BackupPath       = $f.BackupPath
        SizeBytes        = $info.Length
        Sha256           = $hash
        LastWriteTimeUtc = $info.LastWriteTimeUtc.ToString('o')
    }
}

$liveVerification = $null
$watchLog = Join-Path $projectRoot 'ac_launch_logs\process_watch.log'
if (Test-Path -LiteralPath $watchLog) {
    $tail = @(Get-Content -LiteralPath $watchLog -Tail 5)
    if (($tail -join ' ') -match 'RESULT OK acs.exe from AI') {
        $liveVerification = @{
            LogFile  = $watchLog
            Result   = 'OK acs.exe from AI'
            Evidence = $tail
        }
    }
}

$manifest = @{
    SchemaVersion    = 1
    Purpose          = 'pre_csp_ai_backup'
    CreatedAtUtc     = (Get-Date).ToUniversalTime().ToString('o')
    AiInstallPath    = $aiRoot
    SteamInstallPath = $steamRoot
    AcDocumentsPath  = $docsRoot
    LiveVerification = $liveVerification
    InventoryChecked = $inventory
    SteamBaseline    = $steamBaseline
    CopiedFileCount  = $copiedEntries.Count
    Entries          = $copiedEntries
}

$manifestPath = Join-Path $backupDir 'manifest.json'
$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
Write-Host "[OK] Manifeste : $manifestPath"
Write-Host "[OK] Sauvegarde pre-CSP terminee."
