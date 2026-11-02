#Requires -Version 5.1
<#
.SYNOPSIS
    Sauvegarde horodatee pre-plugin (copie AI uniquement).
#>
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string]$BackupRoot,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'Get-AcLocalPaths.ps1')
$paths = Get-AcLocalPaths -RepoRoot $RepoRoot

$aiRoot = $paths.AiInstallPath
$steamRoot = $paths.SteamInstallPath
$projectRoot = $paths.RepoRoot
$pluginSrc = Join-Path $projectRoot 'assetto_corsa_gym\AssettoCorsaPlugin\plugins\sensors_par'
$dllSrc = Join-Path $projectRoot 'assetto_corsa_gym\AssettoCorsaPlugin\windows-libs\DLLs'
$libSrc = Join-Path $projectRoot 'assetto_corsa_gym\AssettoCorsaPlugin\windows-libs\Lib'
$pluginDst = Join-Path $aiRoot 'apps\python\sensors_par'
$dllDst = Join-Path $aiRoot 'system\x64\DLLs'
$libDst = Join-Path $aiRoot 'system\x64\Lib'
$backupRootResolved = if ($BackupRoot) { $BackupRoot } else { Join-Path $projectRoot 'ac_backups' }
$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$backupDir = Join-Path $backupRootResolved "pre_plugin_ai_$timestamp"

$running = @('acs', 'AssettoCorsa', 'acs_x86') | ForEach-Object {
    Get-Process -Name $_ -ErrorAction SilentlyContinue
} | Where-Object { $_ }
if ($running) {
    throw "Fermez Assetto Corsa avant la sauvegarde pre-plugin."
}

function Get-RelativePath([string]$FullPath, [string]$Root) {
    return $FullPath.Substring($Root.Length).TrimStart('\').Replace('\', '/')
}

function Get-FileEntry([string]$SourcePath, [string]$RootPath, [string]$BackupPrefix) {
    $item = Get-Item -LiteralPath $SourcePath
    $rel = Get-RelativePath $item.FullName $RootPath
    return @{
        Scope            = $BackupPrefix
        SourcePath       = $item.FullName
        RelativePath     = $rel
        BackupPath       = ($BackupPrefix + '/' + $rel)
        SizeBytes        = $item.Length
        Sha256           = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash
        LastWriteTimeUtc = $item.LastWriteTimeUtc.ToString('o')
    }
}

$toBackup = @()

if (Test-Path -LiteralPath $pluginDst) {
    Get-ChildItem -LiteralPath $pluginDst -Recurse -File | ForEach-Object {
        $toBackup += Get-FileEntry -SourcePath $_.FullName -RootPath $aiRoot -BackupPrefix 'game_ai'
    }
}

foreach ($pair in @(
    @{ Src = $dllSrc; Dst = $dllDst; Prefix = 'game_ai_dlls' }
    @{ Src = $libSrc; Dst = $libDst; Prefix = 'game_ai_lib' }
)) {
    if (-not (Test-Path -LiteralPath $pair.Src)) { continue }
    Get-ChildItem -LiteralPath $pair.Src -Recurse -File | ForEach-Object {
        $rel = Get-RelativePath $_.FullName $pair.Src
        $dest = Join-Path $pair.Dst $rel
        if (Test-Path -LiteralPath $dest) {
            $toBackup += Get-FileEntry -SourcePath $dest -RootPath $aiRoot -BackupPrefix $pair.Prefix
        }
    }
}

Write-Host "[INFO] Copie AI : $aiRoot"
Write-Host "[INFO] Steam    : $steamRoot (non modifie)"
Write-Host "[INFO] Backup   : $backupDir"
Write-Host "[INFO] Fichiers a sauvegarder : $($toBackup.Count)"

if ($DryRun) {
    $toBackup | ForEach-Object { Write-Host "  $($_.SourcePath)" }
    exit 0
}

New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
foreach ($entry in $toBackup) {
    $dest = [System.IO.Path]::Combine($backupDir, ($entry.BackupPath -replace '/', '\'))
    $parent = Split-Path -Parent $dest
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Copy-Item -LiteralPath $entry.SourcePath -Destination $dest -Force
}

$manifest = @{
    SchemaVersion    = 1
    Purpose          = 'pre_plugin_ai_backup'
    CreatedAtUtc     = (Get-Date).ToUniversalTime().ToString('o')
    AiInstallPath    = $aiRoot
    SteamInstallPath = $steamRoot
    PluginSource     = $pluginSrc
    BackedUpCount    = $toBackup.Count
    Entries          = $toBackup
}
$manifestPath = Join-Path $backupDir 'manifest.json'
$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
Write-Host "[OK] Manifeste : $manifestPath"
Write-Output $backupDir
