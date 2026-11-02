#Requires -Version 5.1
<#
.SYNOPSIS
    Compile le helper Win32 AcWindowPlacementNative et verifie SetWindowPos/GetWindowRect.
#>
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
. (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1')

$fail = @()

if (-not (Initialize-AcWindowPlacementNative)) {
    Write-Host 'FAIL: Initialize-AcWindowPlacementNative returned false'
    exit 1
}

$type = [AcWindowPlacementNative]
if (-not $type) {
    $fail += 'type AcWindowPlacementNative absent'
}

$methods = @($type.GetMethods() | ForEach-Object { $_.Name })
foreach ($name in @('SetWindowPos', 'GetWindowRect', 'MoveWindow', 'ClientToOuter', 'OuterToClient')) {
    if ($methods -notcontains $name) {
        $fail += ('methode absente: ' + $name)
    }
}

$outer = [AcWindowPlacementNative]::ClientToOuter(640, 360)
if ($outer.Width -le 640 -or $outer.Height -le 360) {
    $fail += ('ClientToOuter inattendu: ' + $outer.Width + 'x' + $outer.Height)
}
$roundTrip = [AcWindowPlacementNative]::OuterToClient($outer.Width, $outer.Height)
if ($roundTrip.Width -ne 640 -or $roundTrip.Height -ne 360) {
    $fail += ('OuterToClient inattendu: ' + $roundTrip.Width + 'x' + $roundTrip.Height)
}

$nullRect = Get-AcWindowRectInfo -WindowHandle ([IntPtr]::Zero)
if ($nullRect -ne $null) {
    $fail += 'GetWindowRect handle zero devrait retourner null'
}

$placed = Set-AcWorkerWindowPosition -WindowHandle ([IntPtr]::Zero) -Cell @{
    workerId = 0; screenX = 0; screenY = 0; screenWidth = 100; screenHeight = 100
    outerX = 0; outerY = 0; outerWidth = 100; outerHeight = 100
    renderWidth = 96; renderHeight = 54
}
if ($placed) {
    $fail += 'SetWindowPos handle zero devrait echouer sans exception'
}

if (-not (Initialize-AcWindowPlacementNative)) {
    $fail += 'second Initialize-AcWindowPlacementNative devrait reussir (cache)'
}

if ($fail.Count -gt 0) {
    $fail | ForEach-Object { Write-Host ('FAIL: ' + $_) }
    exit 1
}
Write-Host 'Test-AcWindowPlacementNative: OK'
exit 0
