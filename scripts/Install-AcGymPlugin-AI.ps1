#Requires -Version 5.1
<#
.SYNOPSIS
    Installe le plug-in sensors_par et fusionne DLLs/Lib dans la copie AI uniquement.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [switch]$SkipBackup,
    [switch]$SkipIfUpToDate,
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

if ($aiRoot -eq $steamRoot) {
    throw 'Refus : chemin AI identique a Steam.'
}
if (-not (Test-Path -LiteralPath (Join-Path $aiRoot 'AI_INSTANCE.txt'))) {
    throw "Copie AI invalide : $aiRoot"
}
if (-not (Test-Path -LiteralPath $pluginSrc)) {
    throw "Source plug-in introuvable : $pluginSrc"
}

$backupDir = $null

$stats = @{
    PluginFiles = 0
    DllAdded    = 0
    DllReplaced = 0
    LibAdded    = 0
    LibReplaced = 0
}

function Test-TreeHashesMatch {
    param(
        [Parameter(Mandatory)][string]$SourceRoot,
        [Parameter(Mandatory)][string]$DestRoot
    )
    if (-not (Test-Path -LiteralPath $SourceRoot)) { return $false }
    if (-not (Test-Path -LiteralPath $DestRoot)) { return $false }
    foreach ($item in @(Get-ChildItem -LiteralPath $SourceRoot -Recurse -File)) {
        $rel = $item.FullName.Substring($SourceRoot.Length).TrimStart('\')
        $dest = Join-Path $DestRoot $rel
        if (-not (Test-Path -LiteralPath $dest)) { return $false }
        $srcHash = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash
        $dstHash = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash
        if ($srcHash -ne $dstHash) { return $false }
    }
    return $true
}

function Merge-Tree {
    param(
        [string]$SourceRoot,
        [string]$DestRoot,
        [ref]$Added,
        [ref]$Replaced,
        [switch]$IsDryRun
    )
    Get-ChildItem -LiteralPath $SourceRoot -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($SourceRoot.Length).TrimStart('\')
        $dest = Join-Path $DestRoot $rel
        $exists = Test-Path -LiteralPath $dest
        if ($IsDryRun) {
            if ($exists) {
                $sh = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
                $dh = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash
                if ($sh -ne $dh) { $Replaced.Value++ }
            } else {
                $Added.Value++
            }
            return
        }
        $parent = Split-Path -Parent $dest
        if ($parent -and -not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        if ($exists) {
            $sh = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
            $dh = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash
            if ($sh -eq $dh) { return }
            $Replaced.Value++
        } else {
            $Added.Value++
        }
        Copy-Item -LiteralPath $_.FullName -Destination $dest -Force
    }
}

Write-Host "[INFO] Destination plug-in : $pluginDst"
Write-Host "[INFO] Destination DLLs    : $dllDst"
Write-Host "[INFO] Destination Lib     : $libDst"

if ($DryRun) {
    Write-Host '[DRY-RUN] Installation simulee.'
    if (-not (Test-Path -LiteralPath $pluginDst)) { New-Item -ItemType Directory -Path $pluginDst -Force | Out-Null }
    $stats.PluginFiles = (Get-ChildItem -LiteralPath $pluginSrc -Recurse -File).Count
    Merge-Tree -SourceRoot $dllSrc -DestRoot $dllDst -Added ([ref]$stats.DllAdded) -Replaced ([ref]$stats.DllReplaced) -IsDryRun
    Merge-Tree -SourceRoot $libSrc -DestRoot $libDst -Added ([ref]$stats.LibAdded) -Replaced ([ref]$stats.LibReplaced) -IsDryRun
    Write-Host ($stats | ConvertTo-Json -Compress)
    exit 0
}

if ($SkipIfUpToDate) {
    $pluginOk = Test-TreeHashesMatch -SourceRoot $pluginSrc -DestRoot $pluginDst
    $dllOk = Test-TreeHashesMatch -SourceRoot $dllSrc -DestRoot $dllDst
    $libOk = Test-TreeHashesMatch -SourceRoot $libSrc -DestRoot $libDst
    if ($pluginOk -and $dllOk -and $libOk) {
        Write-Host '[OK] Plug-in deja a jour (hash identique), copie ignoree.'
        exit 0
    }
}

$running = @('acs', 'AssettoCorsa', 'acs_x86') | ForEach-Object {
    Get-Process -Name $_ -ErrorAction SilentlyContinue
} | Where-Object { $_ }
if ($running) {
    $mainPy = Join-Path $pluginDst 'sensors_par.py'
    if ($SkipIfUpToDate -and (Test-Path -LiteralPath $mainPy)) {
        Write-Warning 'AC ouvert: verification hash plugin ignoree, sensors_par.py deja present.'
        exit 0
    }
    throw 'Fermez Assetto Corsa avant installation.'
}

if (-not $SkipBackup) {
    $backupDir = & (Join-Path $scriptDir 'Backup-AcAiPrePlugin.ps1') -RepoRoot $projectRoot | Select-Object -Last 1
}

if (-not (Test-Path -LiteralPath $pluginDst)) {
    New-Item -ItemType Directory -Path $pluginDst -Force | Out-Null
}
Get-ChildItem -LiteralPath $pluginSrc -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($pluginSrc.Length).TrimStart('\')
    $dest = Join-Path $pluginDst $rel
    $parent = Split-Path -Parent $dest
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    if ((Test-Path -LiteralPath $dest) -and ((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash -eq (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash)) {
        return
    }
    Copy-Item -LiteralPath $_.FullName -Destination $dest -Force
    $stats.PluginFiles++
}

Merge-Tree -SourceRoot $dllSrc -DestRoot $dllDst -Added ([ref]$stats.DllAdded) -Replaced ([ref]$stats.DllReplaced)
Merge-Tree -SourceRoot $libSrc -DestRoot $libDst -Added ([ref]$stats.LibAdded) -Replaced ([ref]$stats.LibReplaced)

$luaSrc = Join-Path $projectRoot 'assetto_corsa_gym\AssettoCorsaPlugin\apps\lua\AcAiSpawn'
$luaDst = Join-Path $aiRoot 'apps\lua\AcAiSpawn'
if (Test-Path -LiteralPath $luaSrc) {
    New-Item -ItemType Directory -Force -Path $luaDst | Out-Null
    Copy-Item -Path (Join-Path $luaSrc '*') -Destination $luaDst -Force
}

$mainPy = Join-Path $pluginDst 'sensors_par.py'
if (-not (Test-Path -LiteralPath $mainPy)) {
    throw "Installation incomplete : sensors_par.py absent dans $pluginDst"
}

$steamPlugin = Join-Path $steamRoot 'apps\python\sensors_par'
$steamDll = Join-Path $steamRoot 'system\x64\DLLs'
$steamLib = Join-Path $steamRoot 'system\x64\Lib'
if (Test-Path -LiteralPath $steamPlugin) {
    Write-Warning "ATTENTION : plug-in detecte cote Steam (non modifie par ce script) : $steamPlugin"
}

Write-Host "[OK] Plug-in installe : $pluginDst"
Write-Host "[OK] sensors_par.py   : $mainPy"
Write-Host "[OK] DLLs ajoutes=$($stats.DllAdded) remplaces=$($stats.DllReplaced)"
Write-Host "[OK] Lib  ajoutes=$($stats.LibAdded) remplaces=$($stats.LibReplaced)"
if ($backupDir) { Write-Host "[OK] Sauvegarde : $backupDir" }
Write-Host ($stats | ConvertTo-Json -Compress)
