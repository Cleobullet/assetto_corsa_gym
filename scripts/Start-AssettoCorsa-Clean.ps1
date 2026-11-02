#Requires -Version 5.1
<#
.SYNOPSIS
    Lance Assetto Corsa via l'installation Steam propre (profil Clean).
#>
[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'Get-AcLocalPaths.ps1')
$paths = Get-AcLocalPaths

$steamAc = Join-Path $paths.SteamInstallPath 'AssettoCorsa.exe'
if (-not (Test-Path -LiteralPath $steamAc)) {
    throw "Installation Steam propre introuvable : $steamAc"
}

Write-Host '[INFO] Mode : Clean (Steam original)'
Write-Host "[INFO] Exécutable : $steamAc"

if ($DryRun) {
    Write-Host '[DRY-RUN] Switch-AssettoCorsaProfile.ps1 -Profile Clean'
    Write-Host "[DRY-RUN] Start-Process -FilePath '$steamAc'"
    exit 0
}

& (Join-Path $scriptDir 'Switch-AssettoCorsaProfile.ps1') -Profile Clean -AcDocumentsPath $paths.AcDocumentsPath -RepoRoot $paths.RepoRoot
Start-Process -FilePath $steamAc
Write-Host '[INFO] Assetto Corsa (Steam) lancé.'
