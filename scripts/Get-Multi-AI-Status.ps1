#Requires -Version 5.1
<#
.SYNOPSIS
    Status multi-worker: ports, heartbeat, learner, transitions, poids.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
Set-Location $RepoRoot

. (Join-Path $ScriptDir 'Get-AcLocalPaths.ps1')
. (Join-Path $ScriptDir 'Ac-AiSessionCommon.ps1')

$VenvPython = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
$env:PYTHONPATH = $RepoRoot
$ManifestPath = Join-Path $RepoRoot 'ac_workers\manifest.json'
$WeightsPath = Join-Path $RepoRoot 'outputs\multi_sac_cpu_v3\model\latest.pt'

function Test-TcpListen {
    param([int]$Port)
    try {
        return ($null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue))
    } catch { return $false }
}

if (Test-Path -LiteralPath $ManifestPath) {
    $m = Read-AiSessionManifest -RepoRoot $RepoRoot
    Write-Host ('learner PID: ' + [string]$m.learnerPid)
    $tsLauncher = Get-AiObjectPropertyOrNull -Object $m -Name 'transitionServerLauncherPid'
    if ($tsLauncher) {
        Write-Host ('transition server launcher PID: ' + [string]$tsLauncher)
    }
    Write-Host ('transition server listener PID: ' + [string]$m.transitionServerPid)
    $tsListen = Test-TcpListen -Port 24050
    if (-not $tsListen) {
        Write-Host 'transition server effective_state=offline'
    } else {
        Write-Host 'transition server effective_state=running'
    }
    $learnerStatusPath = Join-Path $RepoRoot 'outputs\multi_sac_cpu_v3\learner_status.json'
    if (Test-Path -LiteralPath $learnerStatusPath) {
        try {
            $ls = Get-Content -LiteralPath $learnerStatusPath -Raw | ConvertFrom-Json
            $learnerPidLive = $false
            if ($ls.learner_pid) {
                $learnerPidLive = $null -ne (Get-Process -Id [int]$ls.learner_pid -ErrorAction SilentlyContinue)
            }
            $learnerActive = $learnerPidLive -and [bool]$ls.learner_running
            if ($learnerActive) {
                Write-Host ('learner effective: active=yes status=running pid=' + $ls.learner_pid)
            } else {
                Write-Host ('learner effective: active=no status=stale saved steps=' + $ls.global_steps + ' replay=' + $ls.replay_size)
            }
        } catch { }
    }
    if ($m.learnerStdoutLog) {
        Write-Host ('  learner stdout: ' + $m.learnerStdoutLog)
    }
    if ($m.learnerStderrLog) {
        Write-Host ('  learner stderr: ' + $m.learnerStderrLog)
        $learnerAlive = $false
        if ($m.learnerPid) {
            $learnerAlive = $null -ne (Get-Process -Id $m.learnerPid -ErrorAction SilentlyContinue)
        }
        if (-not $learnerAlive) {
            Write-Host ('  learner stderr tail: ' + (Get-AiLogTail -LogPath (Join-Path $RepoRoot ($m.learnerStderrLog -replace '/', '\'))))
        }
    }
    foreach ($w in @($m.workers)) {
        $alive = $null -ne (Get-Process -Id $w.acPid -ErrorAction SilentlyContinue)
        $colAlive = $null -ne (Get-Process -Id $w.collectorPid -ErrorAction SilentlyContinue)
        $manifestState = if ($w.state) { [string]$w.state } else { 'unknown' }
        $effectiveState = 'offline'
        if ($alive -and $colAlive) { $effectiveState = 'running' }
        elseif ($w.acPid -or $w.collectorPid) { $effectiveState = 'stale' }
        Write-Host ('=== worker ' + $w.workerId + ' ===')
        Write-Host ('  manifest_state=' + $manifestState + ' effective_state=' + $effectiveState)
        Write-Host ('  AC PID: ' + $w.acPid + ' alive=' + $alive)
        Write-Host ('  collector PID: ' + $w.collectorPid + ' alive=' + $colAlive)
        if ($w.window) {
            Write-Host ('  render: ' + $w.window.renderWidth + 'x' + $w.window.renderHeight)
            Write-Host ('  screen pos: ' + $w.window.screenX + ',' + $w.window.screenY)
            Write-Host ('  screen size: ' + $w.window.screenWidth + 'x' + $w.window.screenHeight)
            Write-Host ('  fullscreen: ' + $w.window.fullscreen)
            Write-Host ('  fps limit: ' + [string]$w.window.fpsLimit)
            Write-Host ('  monitor: ' + $w.window.monitorIndex)
        }
        if ($alive -and $w.acPid) {
            $monIdx = if ($m.monitorIndex) { [int]$m.monitorIndex } else { 0 }
            $liveStatus = Get-AcWorkerWindowStatus -ProcessId $w.acPid -MonitorIndex $monIdx
            if ($null -ne $liveStatus.windowX) {
                Write-Host ('  live window: ' + $liveStatus.windowX + ',' + $liveStatus.windowY `
                    + ' ' + $liveStatus.windowWidth + 'x' + $liveStatus.windowHeight)
            }
        }
        if ($w.collectorStdoutLog) {
            Write-Host ('  collector stdout: ' + $w.collectorStdoutLog)
        }
        if ($w.collectorStderrLog) {
            Write-Host ('  collector stderr: ' + $w.collectorStderrLog)
            if (-not $colAlive) {
                Write-Host ('  collector stderr tail: ' + (Get-AiLogTail -LogPath (Join-Path $RepoRoot ($w.collectorStderrLog -replace '/', '\'))))
            }
        }
        Write-Host ('  ports: ' + $w.ports.ego + '/' + $w.ports.opponents + '/' + $w.ports.mgmt)
        Write-Host ('  vJoy: ' + $w.vjoyDeviceId)
        Write-Host ('  mgmt listen: ' + (Test-TcpListen -Port $w.ports.mgmt))
    }
} else {
    Write-Host 'manifest: absent'
}

if (Test-Path -LiteralPath $VenvPython) {
    $probeJson = & $VenvPython -m ac_workers.status_probe 2>$null
    if ($probeJson) {
        try {
            $probe = $probeJson | ConvertFrom-Json
            if ($probe.learner) {
                Write-Host ('=== learner ===')
                Write-Host ('  global_steps: ' + [string]$probe.learner.global_steps)
                Write-Host ('  replay_size: ' + [string]$probe.learner.replay_size)
                Write-Host ('  updates: ' + [string]$probe.learner.updates)
                Write-Host ('  mean_reward: ' + [string]$probe.learner.mean_reward)
                Write-Host ('  heartbeat_age_sec: ' + [string]$probe.learner.heartbeat_age_sec)
            }
            foreach ($prop in @($probe.workers.PSObject.Properties)) {
                $wid = $prop.Name
                $st = $prop.Value
                Write-Host ('=== worker runtime ' + $wid + ' ===')
                Write-Host ('  transitions: ' + [string]$st.transitions)
                Write-Host ('  speed_kmh: ' + [string]$st.speed_kmh)
                Write-Host ('  reward_last: ' + [string]$st.reward_last)
                Write-Host ('  last_requested: ' + ($st.last_requested_action -join ','))
                Write-Host ('  last_executed: ' + ($st.last_executed_action -join ','))
            }
            if ($probe.metrics) {
                Write-Host ('=== metrics ===')
                Write-Host ('  last_sac_update_ms: ' + [string]$probe.metrics.last_update_ms)
                Write-Host ('  probe_ms: ' + [string]$probe.metrics.probe_ms)
            }
        } catch {
            Write-Host $probeJson
        }
    }
    & $VenvPython -c @"
from common.transition_server import TransitionClient
try:
    s = TransitionClient().stats()
    print('transition queue=', s.get('queue_size'))
    print('learner_connected=', s.get('learner_connected'))
    for wid, st in (s.get('workers') or {}).items():
        hb = (s.get('heartbeats') or {}).get(int(wid) if str(wid).isdigit() else wid)
        print('worker', wid, 'transitions=', st.get('transitions_received'), 'heartbeat=', hb)
except Exception as e:
    print('transition server offline:', e)
"@
}

if (Test-Path -LiteralPath $WeightsPath) {
    Write-Host ('dernier poids: ' + $WeightsPath + ' mtime=' + (Get-Item $WeightsPath).LastWriteTime)
} else {
    Write-Host 'dernier poids: (absent)'
}

& (Join-Path $ScriptDir 'Get-AC-WorkersStatus.ps1')
