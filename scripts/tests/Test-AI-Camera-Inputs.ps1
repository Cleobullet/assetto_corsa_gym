#Requires -Version 5.1
<#
.SYNOPSIS
    Affiche les bindings camera/regard sans lancer AC.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
. (Join-Path $ScriptsRoot 'Get-AcLocalPaths.ps1')
. (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1')

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
}
$paths = Get-AcLocalPaths -RepoRoot $RepoRoot
$Docs = $paths.AcDocumentsPath

$sources = [ordered]@{
    'Documents'          = Join-Path $Docs 'cfg\controls.ini'
    'Worker0 controls'   = Join-Path $RepoRoot 'ac_profiles\workers\worker_0\cfg\controls.ini'
    'Worker1 controls'   = Join-Path $RepoRoot 'ac_profiles\workers\worker_1\cfg\controls.ini'
    'Vjoy_W0'            = Join-Path $Docs 'cfg\controllers\savedsetups\Vjoy_W0.ini'
    'Vjoy_W1'            = Join-Path $Docs 'cfg\controllers\savedsetups\Vjoy_W1.ini'
    'Profil Vjoy_W0'     = Join-Path $RepoRoot 'ac_profiles\workers\worker_0\cfg\controllers\savedsetups\Vjoy_W0.ini'
    'Presentation cam'   = Join-Path $RepoRoot 'ac_profiles\presentation_camera\manifest.json'
}

$sections = Get-AcCameraLookControlSections + @('GLANCELEFT', 'GLANCERIGHT', 'GLANCEBACK')
$sections = @($sections | Select-Object -Unique)
$keys = @('JOY', 'BUTTON', 'KEY', 'XBOXBUTTON', 'AXLE')

Write-Host '=== Test-AI-Camera-Inputs ==='
foreach ($label in $sources.Keys) {
    $path = $sources[$label]
    if (-not (Test-Path -LiteralPath $path)) {
        Write-Host ($label + ': (absent)')
        continue
    }
    if ($path -like '*.json') {
        Write-Host ($label + ': ' + $path)
        Get-Content -LiteralPath $path -Raw | Write-Host
        continue
    }
    Write-Host ('--- ' + $label + ' ---')
    foreach ($section in $sections) {
        $active = @()
        foreach ($key in $keys) {
            $val = Get-IniValue -Path $path -Section $section -Key $key
            if ($val -and $val -ne '-1' -and $val -ne '') {
                $active += ($key + '=' + $val)
            }
        }
        if ($active.Count -gt 0) {
            Write-Host ('  [' + $section + '] ' + ($active -join ' '))
        }
    }
}

$camMgr = Join-Path $Docs 'cfg\camera_manager.ini'
if (Test-Path -LiteralPath $camMgr) {
    $mode = Get-IniValue -Path $camMgr -Section 'LAST_CAMERA' -Key 'MODE'
    Write-Host ('camera_manager.ini LAST_CAMERA MODE=' + [string]$mode)
}

Write-Host ''
$glanceRight = Get-IniValue -Path (Join-Path $Docs 'cfg\controls.ini') -Section 'GLANCERIGHT' -Key 'KEY'
if ($glanceRight -and $glanceRight -ne '-1') {
    Write-Host ('SUSPECT regard droite: controls.ini GLANCERIGHT KEY=' + $glanceRight)
}
Write-Host 'Test-AI-Camera-Inputs: OK'
