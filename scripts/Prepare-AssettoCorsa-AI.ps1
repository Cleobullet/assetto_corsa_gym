#Requires -Version 5.1
<#
.SYNOPSIS
    Prepare copie AI + profil Documents pour session RL.
#>
[CmdletBinding()]
param(
    [switch]$ForceClose,
    [switch]$DryRun,
    [string]$LogPath,
    [string]$RepoRoot,
    [switch]$OnboardCamera
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir 'Get-AcLocalPaths.ps1')
. (Join-Path $ScriptDir 'Ac-AiSessionCommon.ps1')

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
}
Set-Location $RepoRoot

$paths = Get-AcLocalPaths -RepoRoot $RepoRoot
$AiRoot = $paths.AiInstallPath
$SteamRoot = $paths.SteamInstallPath
$Docs = $paths.AcDocumentsPath
$ProfileRoot = Join-Path $RepoRoot 'ac_profiles\ai'

$cfgFiles = @(
    'cfg\python.ini', 'cfg\controls.ini', 'cfg\race.ini', 'cfg\video.ini',
    'cfg\assists.ini', 'cfg\gameplay.ini'
)

function Write-PrepareAction {
    param([string]$Message)
    if ($DryRun) {
        Write-Host ('[DRY-RUN] ' + $Message)
    } else {
        Write-AiSessionLog $Message $LogPath
    }
}

Write-PrepareAction 'Prepare: demarrage'

$running = @(Get-AcRunningProcessNames)
    if ($running.Count -gt 0) {
    if ($DryRun) {
        Write-PrepareAction ('Processus actifs detectes: ' + ($running -join ', ') + ' (non fermes en DryRun)')
    } elseif ($ForceClose) {
        Stop-AcProcessesForce -LogPath $LogPath
    } else {
        throw ('Processus actifs: ' + ($running -join ', ') + '. Fermez-les ou utilisez -ForceClose.')
    }
}

if ($AiRoot -eq $SteamRoot) {
    throw 'Refus: copie AI identique a Steam.'
}
if (-not (Test-Path -LiteralPath (Join-Path $AiRoot 'AI_INSTANCE.txt'))) {
    throw ('Copie AI invalide: ' + $AiRoot)
}

$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$DocBackup = Join-Path $RepoRoot ('ac_profiles\backups\documents_' + $Stamp)

if ($DryRun) {
    Write-PrepareAction ('Backup Documents prevu: ' + $DocBackup)
    foreach ($rel in $cfgFiles) {
        $live = Join-Path $Docs $rel
        if (Test-Path -LiteralPath $live) {
            Write-PrepareAction ('  sauvegarder: ' + $rel)
        }
    }
    $savedLive = Join-Path $Docs 'cfg\controllers\savedsetups'
    if (Test-Path -LiteralPath $savedLive) {
        Write-PrepareAction '  sauvegarder: cfg\controllers\savedsetups'
    }
    Write-PrepareAction ('Install plugin: ' + (Join-Path $ScriptDir 'Install-AcGymPlugin-AI.ps1'))
} else {
    New-Item -ItemType Directory -Path $DocBackup -Force | Out-Null
    foreach ($rel in $cfgFiles) {
        $live = Join-Path $Docs $rel
        if (Test-Path -LiteralPath $live) {
            $bak = Join-Path $DocBackup $rel
            $bakParent = Split-Path -Parent $bak
            if ($bakParent -and -not (Test-Path -LiteralPath $bakParent)) {
                New-Item -ItemType Directory -Path $bakParent -Force | Out-Null
            }
            Copy-Item -LiteralPath $live -Destination $bak -Force
        }
    }
    $savedLive = Join-Path $Docs 'cfg\controllers\savedsetups'
    if (Test-Path -LiteralPath $savedLive) {
        $bakSetups = Join-Path $DocBackup 'cfg\controllers\savedsetups'
        Copy-Item -LiteralPath $savedLive -Destination $bakSetups -Recurse -Force
    }
    Write-AiSessionLog ('Documents sauvegardes: ' + $DocBackup) $LogPath
    Invoke-RequiredPsScript -Path (Join-Path $ScriptDir 'Install-AcGymPlugin-AI.ps1') `
        -Parameters @{ RepoRoot = $RepoRoot; SkipBackup = $true } `
        -ErrorMessage 'Install-AcGymPlugin-AI.ps1 a echoue.'
}

$pluginMain = Join-Path $AiRoot 'apps\python\sensors_par\sensors_par.py'
$pluginCfg = Join-Path $AiRoot 'apps\python\sensors_par\config.py'
foreach ($p in @($pluginMain, $pluginCfg)) {
    if (-not (Test-Path -LiteralPath $p)) {
        throw ('Fichier plugin manquant: ' + $p)
    }
}
Write-PrepareAction 'Plugin sensors_par OK'

if (-not (Test-Path -LiteralPath $ProfileRoot)) {
    throw ('Profil AI absent: ' + $ProfileRoot + '. Lancez Capture-AssettoCorsa-AI-Settings.ps1 d''abord.')
}

$liveSetups = Join-Path $Docs 'cfg\controllers\savedsetups'
$profileSetups = Join-Path $ProfileRoot 'cfg\controllers\savedsetups'

if ($DryRun) {
    foreach ($rel in $cfgFiles) {
        $src = Join-Path $ProfileRoot $rel
        if (Test-Path -LiteralPath $src) {
            Write-PrepareAction ('  appliquer profil: ' + $rel)
        }
    }
    if (Test-Path -LiteralPath $profileSetups) {
        Write-PrepareAction '  copier presets vJoy depuis profil'
    }
    Write-PrepareAction '  python.ini -> section [sensors_par] ACTIVE=1'
    Write-PrepareAction '  race.ini conserve depuis profil AI'
} else {
    foreach ($rel in $cfgFiles) {
        $src = Join-Path $ProfileRoot $rel
        $dst = Join-Path $Docs $rel
        if (Test-Path -LiteralPath $src) {
            $dstParent = Split-Path -Parent $dst
            if ($dstParent -and -not (Test-Path -LiteralPath $dstParent)) {
                New-Item -ItemType Directory -Path $dstParent -Force | Out-Null
            }
            Copy-Item -LiteralPath $src -Destination $dst -Force
        }
    }
    if (Test-Path -LiteralPath $profileSetups) {
        New-Item -ItemType Directory -Path $liveSetups -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $profileSetups '*') -Destination $liveSetups -Recurse -Force
    }
    Write-SensorsParPythonIni -TargetPath (Join-Path $Docs 'cfg\python.ini')
    Write-AiSessionLog 'race.ini conserve depuis profil AI (MODEL/TRACK deja configures)' $LogPath
    if ($OnboardCamera) {
        $camMgrLive = Join-Path $Docs 'cfg\camera_manager.ini'
        $camMgrProf = Join-Path $ProfileRoot 'cfg\camera_manager.ini'
        if (Test-Path -LiteralPath $camMgrProf) {
            Copy-Item -LiteralPath $camMgrProf -Destination $camMgrLive -Force
        } else {
            Set-AcCameraManagerOnboard -TargetPath $camMgrLive
        }
        Write-AiSessionLog 'camera_manager.ini -> onboard (-OnboardCamera)' $LogPath
    }
}

$vjoySource = if ($DryRun) { $profileSetups } else { $liveSetups }
$vjoyLive = @(Get-VjoyPresetFiles -SavedSetupsDir $vjoySource)
if ((Get-CollectionCount $vjoyLive) -eq 0) {
    throw ('Preset vJoy introuvable dans ' + $vjoySource + '. Executez Capture-AssettoCorsa-AI-Settings.ps1 avec une config fonctionnelle.')
}
Write-PrepareAction ('Preset vJoy: ' + $vjoyLive[0].Name)

$pythonIni = Join-Path $Docs 'cfg\python.ini'
if (Test-Path -LiteralPath $pythonIni) {
    $active = Get-IniValue -Path $pythonIni -Section 'sensors_par' -Key 'ACTIVE'
    if ($DryRun -and $active -ne '1') {
        Write-PrepareAction ('  python.ini actuel ACTIVE=' + [string]$active + ' (sera force a 1)')
    }
}

$raceIniCheck = Join-Path $Docs 'cfg\race.ini'
if (Test-Path -LiteralPath $raceIniCheck) {
    $carVal = Get-IniValue -Path $raceIniCheck -Key 'CAR'
    $trackVal = Get-IniValue -Path $raceIniCheck -Key 'TRACK'
    if ($DryRun) {
        Write-PrepareAction ('  race.ini actuel CAR=' + [string]$carVal + ' TRACK=' + [string]$trackVal)
    }
}

Write-PrepareAction ('Prepare: OK (AI=' + $AiRoot + ')')
