#Requires -Version 5.1
<#
.SYNOPSIS
    Capture les reglages AC fonctionnels depuis Documents vers ac_profiles\ai.
#>
[CmdletBinding()]
param(
    [string]$SourceDocuments = 'C:\Users\yadal\OneDrive\Documents\Assetto Corsa',
    [string]$RepoRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir 'Get-AcLocalPaths.ps1')
. (Join-Path $ScriptDir 'Ac-AiSessionCommon.ps1')

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
}

if (-not (Test-Path -LiteralPath $SourceDocuments)) {
    . (Join-Path $ScriptDir 'Get-AcLocalPaths.ps1')
    $paths = Get-AcLocalPaths -RepoRoot $RepoRoot
    $SourceDocuments = $paths.AcDocumentsPath
}

$ProfileRoot = Join-Path $RepoRoot 'ac_profiles\ai'
$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$BackupRoot = Join-Path $RepoRoot ("ac_profiles\captures\{0}" -f $Stamp)

Write-Host "[INFO] Source  : $SourceDocuments"
Write-Host "[INFO] Profil  : $ProfileRoot"
Write-Host "[INFO] Backup  : $BackupRoot"

New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null

$relativeFiles = @(
    'cfg\python.ini',
    'cfg\controls.ini',
    'cfg\race.ini',
    'cfg\video.ini',
    'cfg\assists.ini',
    'cfg\gameplay.ini'
)

$copied = 0
foreach ($rel in $relativeFiles) {
    $src = Join-Path $SourceDocuments $rel
    $dst = Join-Path $ProfileRoot $rel
    if (Copy-FileToProfile -Source $src -Destination $dst -BackupRoot $BackupRoot -RelativePath $rel) {
        Write-Host "  capture: $rel"
        $copied++
    } else {
        Write-Host "  absent : $rel"
    }
}

$savedSetupsSrc = Join-Path $SourceDocuments 'cfg\controllers\savedsetups'
$savedSetupsDst = Join-Path $ProfileRoot 'cfg\controllers\savedsetups'
$vjoyFiles = @(Get-VjoyPresetFiles -SavedSetupsDir $savedSetupsSrc)
if ((Get-CollectionCount $vjoyFiles) -eq 0) {
    Write-Warning "Aucun preset vJoy dans $savedSetupsSrc - rien invente."
} else {
    New-Item -ItemType Directory -Path $savedSetupsDst -Force | Out-Null
    foreach ($f in $vjoyFiles) {
        $dst = Join-Path $savedSetupsDst $f.Name
        Copy-FileToProfile -Source $f.FullName -Destination $dst -BackupRoot $BackupRoot -RelativePath ("cfg\controllers\savedsetups\{0}" -f $f.Name) | Out-Null
        Write-Host "  capture vJoy: $($f.Name)"
        $copied++
    }
}

$pythonIniProfile = Join-Path $ProfileRoot 'cfg\python.ini'
Write-SensorsParPythonIni -TargetPath $pythonIniProfile
Write-Host "  python.ini normalise [sensors_par] ACTIVE=1"

Write-Host ("[OK] Capture terminee ({0} fichiers). Backup: {1}" -f $copied, $BackupRoot)
