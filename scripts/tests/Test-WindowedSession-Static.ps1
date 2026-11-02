#Requires -Version 5.1
<#
.SYNOPSIS
    Audit statique: placement fenetres sans Alt/F1/souris; SWP_NOACTIVATE present.
#>
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$fail = @()

$common = Get-Content -LiteralPath (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1') -Raw
$start = Get-Content -LiteralPath (Join-Path $ScriptsRoot 'Start-Multi-AI-Session.ps1') -Raw

if ($common -notmatch 'AcWindowPlacementNative') {
    $fail += 'AcWindowPlacementNative absent de Ac-AiSessionCommon.ps1'
}
if ($common -notmatch 'Add-Type -TypeDefinition') {
    $fail += 'Add-Type -TypeDefinition absent (helper Win32)'
}
if ($common -match 'MemberDefinition.*using System') {
    $fail += 'MemberDefinition melange avec using System'
}
if ($common -notmatch 'SetWindowPos') {
    $fail += 'SetWindowPos absent'
}
if ($start -notmatch 'Invoke-AcWorkerWindowPlacement') {
    $fail += 'Invoke-AcWorkerWindowPlacement absent de Start-Multi-AI-Session.ps1'
}
if ($start -match 'SetWindowPos.*Focus-WindowWithoutAlt|Focus-WindowWithoutAlt.*SetWindowPos') {
    $fail += 'Focus combine au placement fenetre'
}

$placementBlock = [regex]::Match($common, '(?s)function Set-AcWorkerWindowPosition.*?^}').Value
if ($placementBlock -match 'SendKey|VK_F1|mouse|Mouse|SetCursorPos') {
    $fail += 'Set-AcWorkerWindowPosition contient entree clavier/souris'
}

foreach ($pat in @('VK_F1', 'SendKeys', 'SetCursorPos', 'mouse_event')) {
    if ($placementBlock -match $pat) {
        $fail += ('placement: ' + $pat + ' detecte')
    }
}

if ($fail.Count -gt 0) {
    $fail | ForEach-Object { Write-Host ('FAIL: ' + $_) }
    exit 1
}
Write-Host 'Test-WindowedSession-Static: OK'
exit 0
