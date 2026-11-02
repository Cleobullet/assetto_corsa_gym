#Requires -Version 5.1
<#
.SYNOPSIS
    Preflight hors ligne / live pour pilote multi-worker C4C.
#>
[CmdletBinding()]
param(
    [ValidateSet(1, 2, 3, 4, 5, 6)]
    [int]$Workers = 2,
    [int[]]$OnlyWorkerId,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
Set-Location $RepoRoot

. (Join-Path $ScriptsRoot 'Get-AcLocalPaths.ps1')
. (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1')

$VenvPython = Resolve-AcGymPython -RepoRoot $RepoRoot
$env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"

$HasOnlyWorkerId = $PSBoundParameters.ContainsKey('OnlyWorkerId') -and $null -ne $OnlyWorkerId -and @($OnlyWorkerId).Count -gt 0
$checkIds = if ($HasOnlyWorkerId) { @($OnlyWorkerId) } else { 0..($Workers - 1) }

$fail = $false
foreach ($w in $checkIds) {
    $check = Test-WorkerProfileValid -WorkerId $w -RepoRoot $RepoRoot
    if (-not $check.Ok) {
        Write-Host ('FAIL worker ' + $w + ': ' + $check.Error)
        $fail = $true
        continue
    }
    $ports = Get-WorkerPortMap -WorkerId $w
    if ($ports.VJoy -ne ($w + 1)) {
        Write-Host ('FAIL worker ' + $w + ': vJoy mapping incoherent')
        $fail = $true
    }
}
if (-not $HasOnlyWorkerId -and $Workers -eq 6) {
    $seen = @{}
    foreach ($w in 0..5) {
        $p = Get-WorkerPortMap -WorkerId $w
        foreach ($key in @('Ego', 'Opp', 'Mgmt', 'VJoy')) {
            $val = $p[$key]
            if ($seen.ContainsKey($val)) {
                Write-Host ('FAIL port/vJoy duplique: ' + $val)
                $fail = $true
            }
            $seen[$val] = $true
        }
    }
}

if ($DryRun) {
    Write-Host ('[DRY-RUN] Workers=' + $Workers + ' configs OK=' + (-not $fail))
    if ($fail) { exit 1 }
    exit 0
}

# vJoy device availability: each worker N needs vJoy device N+1 enabled in the driver.
# status 3 = VJD_STAT_MISS (device not enabled in vJoy config) -> fast, actionable fail.
$vjoyIds = ($checkIds | ForEach-Object { $_ + 1 }) -join ','
$vjoyPy = @"
import sys
from ac_workers.vjoy_session import get_vjd_status
ids = [int(x) for x in '$vjoyIds'.split(',') if x.strip()]
miss = [d for d in ids if get_vjd_status(d) == 3]
if miss:
    print('VJOY_MISS ' + ','.join(str(d) for d in miss))
    sys.exit(2)
print('VJOY_OK')
"@
$vjoyCheck = Invoke-PythonCapturedProcess -PythonExe $VenvPython -ArgumentList @('-c', $vjoyPy) `
    -WorkingDirectory $RepoRoot -TimeoutSec 20
$vjoyLine = ($vjoyCheck.Stdout -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
if ($vjoyLine -match 'VJOY_MISS\s+(.+)') {
    $missing = $Matches[1].Trim()
    Write-Host ('FAIL vJoy device(s) non active(s) dans le driver: ' + $missing)
    Write-Host '  -> Ouvrez "Configure vJoy", activez les devices 1 a 6 (Number of Devices=6),'
    Write-Host '     chaque device avec axes X/Y/Z + au moins 1 bouton, puis Apply.'
    $fail = $true
}

$pluginCfg = Join-Path (Get-AcLocalPaths -RepoRoot $RepoRoot).AiInstallPath 'apps\python\sensors_par\config.py'
if (Test-Path -LiteralPath $pluginCfg) {
    $txt = Get-Content -LiteralPath $pluginCfg -Raw
    if ($txt -notmatch 'enable_debug_hotkeys = False') {
        Write-Host 'FAIL plugin enable_debug_hotkeys not False'
        $fail = $true
    }
}

if ($fail) {
    Write-Host 'Test-Multi-AI-Preflight: FAIL'
    exit 1
}
Write-Host 'Test-Multi-AI-Preflight: OK'
exit 0
