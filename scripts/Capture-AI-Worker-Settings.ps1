#Requires -Version 5.1
<#
.SYNOPSIS
    Capture Documents -> ac_profiles/workers/worker_N.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet(0, 1, 2, 3, 4, 5)]
    [int]$WorkerId,
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
$paths = Get-AcLocalPaths -RepoRoot $RepoRoot
$ProfileRoot = Join-Path $RepoRoot ('ac_profiles\workers\worker_' + $WorkerId)
$BackupRoot = Join-Path $RepoRoot ('ac_profiles\captures\worker_' + $WorkerId + '_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
$Docs = $paths.AcDocumentsPath

New-Item -ItemType Directory -Path $ProfileRoot -Force | Out-Null
New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null

$relFiles = @(Get-AcWorkerCfgFiles) + @(Get-AcCameraProfileFiles)
foreach ($rel in $relFiles) {
    $src = Join-Path $Docs $rel
    $dst = Join-Path $ProfileRoot $rel
    if (Copy-FileToProfile -Source $src -Destination $dst -BackupRoot $BackupRoot -RelativePath $rel) {
        Write-Host ('  capture: ' + $rel)
    }
}

$vjoyName = Get-WorkerVjoyPresetName -WorkerId $WorkerId
$setupsSrc = Join-Path $Docs 'cfg\controllers\savedsetups'
$setupsDst = Join-Path $ProfileRoot 'cfg\controllers\savedsetups'
New-Item -ItemType Directory -Path $setupsDst -Force | Out-Null
$vjoyFiles = @(Get-VjoyPresetFiles -SavedSetupsDir $setupsSrc)
foreach ($f in $vjoyFiles) {
    $dst = Join-Path $setupsDst $vjoyName
    Copy-FileToProfile -Source $f.FullName -Destination $dst -BackupRoot $BackupRoot -RelativePath ('cfg\controllers\savedsetups\' + $vjoyName) | Out-Null
    Write-Host ('  vJoy preset -> ' + $vjoyName)
    break
}
Invoke-SyncWorkerGearMapping -WorkerId $WorkerId -DocumentsRoot $ProfileRoot -RepoRoot $RepoRoot
Write-SensorsParPythonIni -TargetPath (Join-Path $ProfileRoot 'cfg\python.ini')
Write-Host ('[OK] Profil worker ' + $WorkerId + ' -> ' + $ProfileRoot)
