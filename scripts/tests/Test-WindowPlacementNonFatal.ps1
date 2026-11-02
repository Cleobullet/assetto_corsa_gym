#Requires -Version 5.1
<#
.SYNOPSIS
    Verifie que les erreurs de placement n'interrompent pas le worker (non-fatal).
#>
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
. (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1')

$fail = @()
$start = Get-Content -LiteralPath (Join-Path $ScriptsRoot 'Start-Multi-AI-Session.ps1') -Raw
$common = Get-Content -LiteralPath (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1') -Raw

if ($start -notmatch 'window placement skipped \(non-fatal\)') {
    $fail += 'Start-Multi-AI-Session ne journalise pas placement non-fatal'
}
if ($common -notmatch 'window placement unavailable') {
    $fail += 'Invoke-AcWorkerWindowPlacement ne journalise pas indisponibilite'
}

$invokeBlock = [regex]::Match($common, '(?s)function Invoke-AcWorkerWindowPlacement.*?catch\s*\{').Value
if (-not $invokeBlock) {
    $fail += 'Invoke-AcWorkerWindowPlacement sans try/catch'
}

$result = Invoke-AcWorkerWindowPlacement -Process $null -Cell @{
    workerId = 0; screenX = 0; screenY = 0; screenWidth = 100; screenHeight = 100
    outerX = 0; outerY = 0; outerWidth = 100; outerHeight = 100
    renderWidth = 96; renderHeight = 54
}
if ($result -ne $false) {
    $fail += 'Process null devrait retourner false sans exception'
}

if ($fail.Count -gt 0) {
    $fail | ForEach-Object { Write-Host ('FAIL: ' + $_) }
    exit 1
}
Write-Host 'Test-WindowPlacementNonFatal: OK'
exit 0
