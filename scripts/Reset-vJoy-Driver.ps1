#Requires -Version 5.1
<#
.SYNOPSIS
    Reinitialise le driver vJoy (disable/enable PnP) pour liberer les devices bloques.
#>
[CmdletBinding()]
param(
    [int]$DeviceCount = 6,
    [string[]]$InstanceId
)

$ErrorActionPreference = 'Continue'

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

if (-not (Test-IsAdmin)) {
    Write-Host 'Elevation requise (UAC)...'
    $self = $MyInvocation.MyCommand.Path
    Start-Process powershell -Verb RunAs -Wait -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $self, '-DeviceCount', $DeviceCount
    )
    exit $LASTEXITCODE
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir '..')).Path
$venvPython = Join-Path $repoRoot '.venv-acgym\Scripts\python.exe'
$verifyScript = Join-Path $repoRoot 'ac_workers\verify_vjoy_devices.py'

Write-Host 'Arret des process AC / collectors...' -ForegroundColor Cyan
Get-Process acs -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

function Get-VjoyPnpDevices {
    Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object {
        $_.FriendlyName -match 'vJoy'
    }
}

if ($InstanceId) {
    $targets = @($InstanceId | ForEach-Object {
        [pscustomobject]@{ FriendlyName = $_; InstanceId = $_ }
    })
} else {
    $targets = @(Get-VjoyPnpDevices)
}

if ($targets.Count -eq 0) {
    Write-Host 'ERREUR: aucun device vJoy trouve via PnP.' -ForegroundColor Red
    exit 2
}

Write-Host ('Noeuds vJoy detectes: ' + $targets.Count) -ForegroundColor Cyan
foreach ($t in $targets) {
    Write-Host ('  - ' + $t.FriendlyName + '  [' + $t.InstanceId + ']')
}

function Invoke-PnpToggle {
    param(
        [string]$Action,
        [string]$FriendlyName,
        [string]$Id
    )
    $ok = $false
    try {
        if ($Action -eq 'disable') {
            Disable-PnpDevice -InstanceId $Id -Confirm:$false -ErrorAction Stop | Out-Null
        } else {
            Enable-PnpDevice -InstanceId $Id -Confirm:$false -ErrorAction Stop | Out-Null
        }
        $ok = $true
    } catch {
        Write-Host ('  cmdlet ' + $Action + ' ignore: ' + $_.Exception.Message) -ForegroundColor DarkYellow
    }
    if (-not $ok) {
        $pnputil = Join-Path $env:SystemRoot 'System32\pnputil.exe'
        if (Test-Path -LiteralPath $pnputil) {
            $arg = if ($Action -eq 'disable') { '/disable-device' } else { '/enable-device' }
            $out = & $pnputil $arg $Id 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Host ('  pnputil ' + $Action + ' OK: ' + $FriendlyName) -ForegroundColor Green
                $ok = $true
            } else {
                Write-Host ('  pnputil ' + $Action + ' echec: ' + ($out -join ' ')) -ForegroundColor DarkYellow
            }
        }
    }
    if ($ok -and $Action -eq 'enable') {
        Write-Host ($Action + ': ' + $FriendlyName) -ForegroundColor Green
    } elseif ($ok) {
        Write-Host ($Action + ': ' + $FriendlyName) -ForegroundColor Green
    }
    return $ok
}

# PDO brut d'abord (handles feeder), puis HID.
$sortedDisable = $targets | Sort-Object {
    if ($_.InstanceId -match 'VJOYRAWPDO') { 0 } else { 1 }
}
foreach ($t in $sortedDisable) {
    [void](Invoke-PnpToggle -Action 'disable' -FriendlyName $t.FriendlyName -Id $t.InstanceId)
}
Start-Sleep -Seconds 3

$sortedEnable = $targets | Sort-Object {
    if ($_.InstanceId -match 'VJOYRAWPDO') { 0 } else { 1 }
}
foreach ($t in $sortedEnable) {
    [void](Invoke-PnpToggle -Action 'enable' -FriendlyName $t.FriendlyName -Id $t.InstanceId)
}
Start-Sleep -Seconds 3

# Restart-device sur le PDO (souvent plus fiable que disable/enable seul).
$pdo = $targets | Where-Object { $_.InstanceId -match 'VJOYRAWPDO' } | Select-Object -First 1
if ($pdo) {
    $pnputil = Join-Path $env:SystemRoot 'System32\pnputil.exe'
    if (Test-Path -LiteralPath $pnputil) {
        Write-Host ('pnputil /restart-device: ' + $pdo.FriendlyName) -ForegroundColor Cyan
        & $pnputil '/restart-device' $pdo.InstanceId 2>&1 | Out-Null
        Start-Sleep -Seconds 3
    }
}

Write-Host 'vJoy driver reset termine.' -ForegroundColor Green

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host 'Verification ignoree: venv introuvable.' -ForegroundColor DarkYellow
    exit 0
}
if (-not (Test-Path -LiteralPath $verifyScript)) {
    Write-Host 'Verification ignoree: verify_vjoy_devices.py introuvable.' -ForegroundColor DarkYellow
    exit 0
}

Write-Host 'Verification acquisition des devices...' -ForegroundColor Cyan
$env:PYTHONPATH = "$repoRoot;$repoRoot\assetto_corsa_gym"
$env:VJOY_VERIFY_COUNT = "$DeviceCount"
Push-Location $repoRoot
& $venvPython $verifyScript
$code = $LASTEXITCODE
Pop-Location

if ($code -eq 0) {
    Write-Host ('OK: les ' + $DeviceCount + ' devices vJoy sont acquerables.') -ForegroundColor Green
} else {
    Write-Host 'ATTENTION: certains devices restent bloques. Redemarrage PC recommande.' -ForegroundColor Red
}
exit $code
