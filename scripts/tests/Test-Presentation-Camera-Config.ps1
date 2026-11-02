#Requires -Version 5.1
<#
.SYNOPSIS
    Verification statique camera presentation plugin_direct (hors ligne).
#>
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
. (Join-Path $ScriptsRoot 'Get-AcLocalPaths.ps1')
. (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1')

$fail = @()
$cfg = Get-AcPresentationCameraConfig -RepoRoot $RepoRoot
$cfgJson = Get-Content -LiteralPath (Get-AcPresentationCameraConfigPath -RepoRoot $RepoRoot) -Raw | ConvertFrom-Json

Write-Host ('method=' + $cfg.Method)
Write-Host ('cameraMode=' + $cfg.CameraMode)
Write-Host ('cameraModeId=' + $cfg.CameraModeId)
Write-Host ('drivableCameraIndex=' + $cfg.DrivableCameraIndex)

if ($cfg.Method -ne 'plugin_direct') { $fail += 'method != plugin_direct' }
if ($cfg.CameraMode -ne 'Drivable') { $fail += 'cameraMode != Drivable' }
if ($cfg.CameraModeId -ne 2) { $fail += 'cameraModeId != 2' }
if ($cfg.DrivableCameraIndex -ne 0 -and $cfg.DrivableCameraIndex -ne 1) { $fail += 'drivableCameraIndex invalid' }
if ($cfgJson.cameraModeId -eq 3) { $fail += 'Track cameraModeId=3 in config' }

$commonText = Get-Content -LiteralPath (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1') -Raw
if ($commonText -match 'Send-AcFunctionKeyPress|Invoke-AcThirdPersonCameraSelect|f1PressesAfterDrive') {
    $fail += 'legacy F1/camera select still referenced in Ac-AiSessionCommon.ps1'
}
if ($commonText -match 'MODE=3') {
    $fail += 'MODE=3 still written in Ac-AiSessionCommon.ps1'
}

$pluginPy = Join-Path $RepoRoot 'assetto_corsa_gym\AssettoCorsaPlugin\plugins\sensors_par\sensors_par.py'
$pluginText = Get-Content -LiteralPath $pluginPy -Raw
if ($pluginText -notmatch 'ApplyPresentationCamera') { $fail += 'ApplyPresentationCamera missing' }
if ($pluginText -notmatch 'acsys\.CM\.Drivable') { $fail += 'CM.Drivable missing' }
if ($pluginText -notmatch 'ext_setCurrentDrivableCamera') { $fail += 'ext_setCurrentDrivableCamera missing' }

$configPy = Join-Path $RepoRoot 'assetto_corsa_gym\AssettoCorsaPlugin\plugins\sensors_par\config.py'
$configText = Get-Content -LiteralPath $configPy -Raw
if ($configText -notmatch 'presentation_mode') { $fail += 'presentation_mode missing in config.py' }
if ($configText -notmatch 'presentation_drivable_index') { $fail += 'presentation_drivable_index missing' }

$paths = Get-AcLocalPaths -RepoRoot $RepoRoot
$tempDir = Join-Path $env:TEMP ('ac_cam_test_' + [guid]::NewGuid().ToString('N'))
$tempDocs = Join-Path $tempDir 'docs'
New-Item -ItemType Directory -Path (Join-Path $tempDocs 'cfg\controllers\savedsetups') -Force | Out-Null
$srcControls = Join-Path $paths.AcDocumentsPath 'cfg\controls.ini'
$srcVjoy = Join-Path $paths.AcDocumentsPath 'cfg\controllers\savedsetups\Vjoy_W0.ini'
if (Test-Path -LiteralPath $srcControls) {
    Copy-Item -LiteralPath $srcControls -Destination (Join-Path $tempDocs 'cfg\controls.ini') -Force
}
if (Test-Path -LiteralPath $srcVjoy) {
    Copy-Item -LiteralPath $srcVjoy -Destination (Join-Path $tempDocs 'cfg\controllers\savedsetups\Vjoy_W0.ini') -Force
}
Apply-AI-Presentation-CameraConfig -Docs $tempDocs -RepoRoot $RepoRoot -PresentationMode -PresentationCamera ThirdPerson -PresentationChaseIndex 0 | Out-Null
Set-AcPresentationCameraLookNeutral -Docs $tempDocs -RepoRoot $RepoRoot -WorkerId 0 | Out-Null

$pyIni = Join-Path $tempDocs 'cfg\python.ini'
if ((Get-IniValue -Path $pyIni -Section 'sensors_par' -Key 'PRESENTATION_MODE') -ne '1') {
    $fail += 'PRESENTATION_MODE not set in python.ini'
}
if ((Get-IniValue -Path $pyIni -Section 'sensors_par' -Key 'PRESENTATION_CAMERA') -ne 'ThirdPerson') {
    $fail += 'PRESENTATION_CAMERA not ThirdPerson'
}
if ((Get-IniValue -Path $pyIni -Section 'sensors_par' -Key 'PRESENTATION_DRIVABLE_INDEX') -ne '0') {
    $fail += 'PRESENTATION_DRIVABLE_INDEX != 0'
}

$lookOk = $true
foreach ($section in (Get-AcCameraLookControlSections)) {
    $val = Get-IniValue -Path (Join-Path $tempDocs 'cfg\controls.ini') -Section $section -Key 'KEY'
    if ($val -and $val -ne '-1' -and $val -ne '') { $lookOk = $false }
}
Write-Host ('look bindings neutralized=' + $lookOk)
if (-not $lookOk) { $fail += 'look bindings not neutralized' }

$vjoyTest = Join-Path $tempDocs 'cfg\controllers\savedsetups\Vjoy_W0.ini'
if (Test-Path -LiteralPath $vjoyTest) {
    if ((Get-IniValue -Path $vjoyTest -Section 'STEER' -Key 'JOY') -eq '-1') { $fail += 'STEER neutralized' }
    if ((Get-IniValue -Path $vjoyTest -Section 'THROTTLE' -Key 'AXLE') -eq '-1') { $fail += 'THROTTLE neutralized' }
}

Write-Host 'F1 simulation=false'
Write-Host 'Track camera=false'
& (Join-Path $ScriptDir 'Test-Audit-No-AltKeys.ps1') | Out-Null
if ($LASTEXITCODE -ne 0) {
    $fail += 'Alt simulation detected'
} else {
    Write-Host 'Alt simulation=false'
}

Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue

if ($fail.Count -gt 0) {
    $fail | ForEach-Object { Write-Host ('FAIL: ' + $_) }
    exit 1
}
Write-Host 'Test-Presentation-Camera-Config: OK'
exit 0
