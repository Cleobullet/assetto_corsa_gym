#Requires -Version 5.1
<#
.SYNOPSIS
    Service janitor vJoy privilegie (tourne en SYSTEM via tache planifiee).
.DESCRIPTION
    Boucle longue duree: ecrit un heartbeat, surveille un fichier de requete depose
    par le Control Center (non privilegie) et execute un reset PnP global du driver
    vJoy (disable/enable du PDO brut partage -> remet a zero les 6 devices) puis
    verifie que tous les devices sont acquerables. Permet la recuperation des handles
    vJoy bloques SANS prompt UAC a chaque fois (l'UAC n'arrive qu'une fois, a
    l'installation de la tache).
#>
[CmdletBinding()]
param(
    [int]$DeviceCount = 6,
    [double]$HeartbeatSec = 2.0,
    [string]$RepoRoot
)

$ErrorActionPreference = 'Continue'

if (-not $RepoRoot) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $RepoRoot = (Resolve-Path (Join-Path $scriptDir '..')).Path
}

$ipcDir = Join-Path $RepoRoot 'ac_workers\.vjoy_janitor'
$heartbeatPath = Join-Path $ipcDir 'heartbeat.json'
$requestPath = Join-Path $ipcDir 'request.json'
$resultPath = Join-Path $ipcDir 'result.json'
$logPath = Join-Path $ipcDir 'janitor.log'
$venvPython = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
$verifyScript = Join-Path $RepoRoot 'ac_workers\verify_vjoy_devices.py'

if (-not (Test-Path -LiteralPath $ipcDir)) {
    New-Item -ItemType Directory -Force -Path $ipcDir | Out-Null
}

function Write-JanitorLog {
    param([string]$Message)
    try {
        $ts = (Get-Date).ToString('s')
        Add-Content -LiteralPath $logPath -Value ($ts + ' ' + $Message) -Encoding UTF8
    } catch { }
}

function Get-UnixSeconds {
    return [Math]::Round([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0, 3)
}

function Write-JsonAtomic {
    param([string]$Path, $Object)
    $json = $Object | ConvertTo-Json -Depth 6 -Compress
    $tmp = $Path + '.tmp'
    # UTF8NoBOM: Python json.loads() fails on BOM and janitor_alive() stays false forever.
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($tmp, $json, $utf8NoBom)
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Write-Heartbeat {
    Write-JsonAtomic -Path $heartbeatPath -Object ([ordered]@{
        pid     = $PID
        ts      = (Get-UnixSeconds)
        version = 1
    })
}

function Get-VjoyPnpDevices {
    Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object { $_.FriendlyName -match 'vJoy' }
}

function Invoke-PnpToggle {
    param([string]$Action, [string]$Id)
    $ok = $false
    try {
        if ($Action -eq 'disable') {
            Disable-PnpDevice -InstanceId $Id -Confirm:$false -ErrorAction Stop | Out-Null
        } else {
            Enable-PnpDevice -InstanceId $Id -Confirm:$false -ErrorAction Stop | Out-Null
        }
        $ok = $true
    } catch {
        $pnputil = Join-Path $env:SystemRoot 'System32\pnputil.exe'
        if (Test-Path -LiteralPath $pnputil) {
            $arg = if ($Action -eq 'disable') { '/disable-device' } else { '/enable-device' }
            & $pnputil $arg $Id 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) { $ok = $true }
        }
    }
    return $ok
}

function Invoke-VjoyDriverReset {
    $targets = @(Get-VjoyPnpDevices)
    if ($targets.Count -eq 0) {
        return @{ ok = $false; message = 'no vJoy PnP node found' }
    }
    # PDO brut d'abord (handles feeder bloques), puis HID.
    $sorted = $targets | Sort-Object { if ($_.InstanceId -match 'VJOYRAWPDO') { 0 } else { 1 } }
    foreach ($t in $sorted) { [void](Invoke-PnpToggle -Action 'disable' -Id $t.InstanceId) }
    Start-Sleep -Seconds 3
    foreach ($t in $sorted) { [void](Invoke-PnpToggle -Action 'enable' -Id $t.InstanceId) }
    Start-Sleep -Seconds 3
    $pdo = $targets | Where-Object { $_.InstanceId -match 'VJOYRAWPDO' } | Select-Object -First 1
    if ($pdo) {
        $pnputil = Join-Path $env:SystemRoot 'System32\pnputil.exe'
        if (Test-Path -LiteralPath $pnputil) {
            & $pnputil '/restart-device' $pdo.InstanceId 2>&1 | Out-Null
            Start-Sleep -Seconds 3
        }
    }
    return @{ ok = $true; message = ('reset ' + $targets.Count + ' vJoy node(s)') }
}

function Test-VjoyAcquirable {
    param([int]$Count)
    if (-not (Test-Path -LiteralPath $venvPython) -or -not (Test-Path -LiteralPath $verifyScript)) {
        return @{ verified = $false; message = 'verify skipped (venv or script missing)' }
    }
    $env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
    $env:VJOY_VERIFY_COUNT = "$Count"
    Push-Location $RepoRoot
    & $venvPython $verifyScript 2>&1 | Out-Null
    $code = $LASTEXITCODE
    Pop-Location
    return @{ verified = ($code -eq 0); message = ('verify exit=' + $code) }
}

Write-JanitorLog ('janitor started pid=' + $PID + ' repo=' + $RepoRoot)
$lastRequestId = $null
Write-Heartbeat

while ($true) {
    Write-Heartbeat
    try {
        if (Test-Path -LiteralPath $requestPath) {
            $req = $null
            try { $req = Get-Content -LiteralPath $requestPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $req = $null }
            if ($req -and $req.request_id -and ($req.request_id -ne $lastRequestId)) {
                $lastRequestId = $req.request_id
                $count = if ($req.devices) { @($req.devices).Count } else { $DeviceCount }
                if ($count -lt 1) { $count = $DeviceCount }
                Write-JanitorLog ('processing request ' + $req.request_id + ' devices=' + $count)
                $reset = Invoke-VjoyDriverReset
                $verify = Test-VjoyAcquirable -Count $count
                $result = [ordered]@{
                    request_id = $req.request_id
                    ok         = [bool]$reset.ok
                    verified   = [bool]$verify.verified
                    message    = ($reset.message + '; ' + $verify.message)
                    ts         = (Get-UnixSeconds)
                    version    = 1
                }
                Write-JsonAtomic -Path $resultPath -Object $result
                Write-JanitorLog ('result ' + $req.request_id + ' ok=' + $reset.ok + ' verified=' + $verify.verified)
            }
        }
    } catch {
        Write-JanitorLog ('loop error: ' + $_.Exception.Message)
    }
    Start-Sleep -Seconds $HeartbeatSec
}
