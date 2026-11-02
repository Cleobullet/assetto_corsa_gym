#Requires -Version 5.1
<#
.SYNOPSIS
    Pilote multi-worker C4C: profils sequentiels, handshake, learner central.
#>
[CmdletBinding()]
param(
    [ValidateSet(1, 2, 3, 4, 5, 6)]
    [int]$Workers = 2,
    [switch]$PresentationMode,
    [ValidateSet('ThirdPerson')]
    [string]$PresentationCamera,
    [ValidateSet(0, 1)]
    [int]$PresentationChaseIndex = 0,
    [switch]$Windowed,
    [ValidateSet('Auto', 'Grid', 'Legacy')]
    [string]$WindowLayout = 'Legacy',
    [int]$MonitorIndex = 0,
    [ValidateRange(0, 6)]
    [int]$GridWorkerCount = 0,
    [int]$WindowGap = 4,
    [ValidateSet('Default', 'SixInstances')]
    [string]$RenderPreset = 'Default',
    [int]$WindowWidth = 0,
    [int]$WindowHeight = 0,
    [switch]$DryRun,
    [switch]$OnboardCamera,
    [switch]$StartLearner,
    [switch]$RequireCollectorReady,
    [int[]]$OnlyWorkerId,
    [switch]$SkipVjoyIndexProbe
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
Set-Location $RepoRoot

. (Join-Path $ScriptDir 'Get-AcLocalPaths.ps1')
. (Join-Path $ScriptDir 'Ac-AiSessionCommon.ps1')

$paths = Get-AcLocalPaths -RepoRoot $RepoRoot
$AiRoot = $paths.AiInstallPath
$Docs = $paths.AcDocumentsPath
$LogDir = Join-Path $RepoRoot 'ac_workers\logs'
$SessionStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$OrchestratorLog = Join-Path $LogDir ('multi_session_' + $SessionStamp + '.log')
$TransitionStdoutLog = Join-Path $LogDir ('transition_' + $SessionStamp + '_stdout.log')

# Prefer UI settings (Paramètres) when window size was left at default 0.
$uiSettings = Get-AcUiSettings -RepoRoot $RepoRoot
if ($null -ne $uiSettings) {
    if ($WindowWidth -le 0 -and $uiSettings.WorkerWidth) {
        $WindowWidth = [int]$uiSettings.WorkerWidth
    }
    if ($WindowHeight -le 0 -and $uiSettings.WorkerHeight) {
        $WindowHeight = [int]$uiSettings.WorkerHeight
    }
}
Sync-WorkerRaceFromUiSettings -RepoRoot $RepoRoot -DryRun:$DryRun | Out-Null
$TransitionStderrLog = Join-Path $LogDir ('transition_' + $SessionStamp + '_stderr.log')
$VenvPython = Resolve-AcGymPython -RepoRoot $RepoRoot
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

Write-AiSessionLog '=== Start-Multi-AI-Session C4C ===' $OrchestratorLog

trap {
    $fatal = $_.Exception.Message
    if ($OrchestratorLog) {
        Write-AiSessionLog ('FATAL: ' + $fatal) $OrchestratorLog
    }
    Write-Host ('FATAL: ' + $fatal)
    [Console]::Error.WriteLine($fatal)
    exit 1
}

$HasOnlyWorkerId = $PSBoundParameters.ContainsKey('OnlyWorkerId') -and $null -ne $OnlyWorkerId -and @($OnlyWorkerId).Count -gt 0
$workerIds = if ($HasOnlyWorkerId) { @($OnlyWorkerId) } else { 0..($Workers - 1) }
$effectiveWorkerCount = if ($HasOnlyWorkerId) { @($OnlyWorkerId).Count } else { $Workers }

$windowGrid = $null
if ($Windowed) {
    # When GridWorkerCount is set, grid geometry is driven by the full worker set so
    # each worker keeps its fixed cell (0=top-left, 1=top-middle, ...) even when
    # launched alone via -OnlyWorkerId. Otherwise we keep the legacy compact grid.
    if ($GridWorkerCount -gt 0) {
        $windowGrid = Get-AcWindowGridLayout -WorkerCount $GridWorkerCount `
            -MonitorIndex $MonitorIndex -WindowGap $WindowGap -WindowLayout $WindowLayout `
            -WindowWidth $WindowWidth -WindowHeight $WindowHeight
    } else {
        $windowGrid = Get-AcWindowGridLayout -WorkerCount $effectiveWorkerCount `
            -MonitorIndex $MonitorIndex -WindowGap $WindowGap -WindowLayout $WindowLayout `
            -WindowWidth $WindowWidth -WindowHeight $WindowHeight
        $gridCells = @($windowGrid.cells)
        for ($ci = 0; $ci -lt $gridCells.Count -and $ci -lt @($workerIds).Count; $ci++) {
            $gridCells[$ci].workerId = @($workerIds)[$ci]
        }
    }
    Write-AiSessionLog (
        'Windowed grid ' + $windowGrid.cols + 'x' + $windowGrid.rows `
        + ' monitor=' + $MonitorIndex + ' gap=' + $WindowGap + ' preset=' + $RenderPreset
    ) $OrchestratorLog
    if ($DryRun) {
        foreach ($cell in @($windowGrid.cells)) {
            Write-Host (
                '[DRY-RUN] cell worker ' + $cell.workerId + ': screen ' `
                + $cell.screenX + ',' + $cell.screenY + ' ' + $cell.screenWidth + 'x' + $cell.screenHeight `
                + ' render ' + $cell.renderWidth + 'x' + $cell.renderHeight
            )
        }
    }
    $bgPolicy = Test-AcBackgroundExecutionPolicy -RepoRoot $RepoRoot -Docs $Docs
    if (-not $bgPolicy.Ok) {
        Write-AiSessionLog ('WARN background: ' + $bgPolicy.Warning) $OrchestratorLog
        Write-Host ('WARN: ' + $bgPolicy.Warning)
    }
}

foreach ($w in $workerIds) {
    $check = Test-WorkerProfileValid -WorkerId $w -RepoRoot $RepoRoot
    if (-not $check.Ok) {
        if ($DryRun) {
            Write-Host ('[DRY-RUN] WARN worker ' + $w + ': ' + $check.Error)
        } else {
            throw $check.Error
        }
    }
}

if (-not $DryRun) {
    if ($HasOnlyWorkerId) {
        & (Join-Path $ScriptDir 'tests\Test-Multi-AI-Preflight.ps1') -OnlyWorkerId @($workerIds)
        if ($LASTEXITCODE -ne 0) { throw 'Preflight multi-AI echoue.' }
        Write-AiSessionLog 'collector import preflight skipped (incremental -OnlyWorkerId)' $OrchestratorLog
    } else {
        & (Join-Path $ScriptDir 'tests\Test-Multi-AI-Preflight.ps1') -Workers $Workers
        if ($LASTEXITCODE -ne 0) { throw 'Preflight multi-AI echoue.' }
        & (Join-Path $ScriptDir 'tests\Test-C4D-Collector-Import.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Collector import preflight echoue.' }
    }
}

$DocBackup = Join-Path $RepoRoot ('ac_profiles\backups\multi_' + $SessionStamp)
Backup-AcDocumentsForMultiWorker -Docs $Docs -BackupRoot $DocBackup -DryRun:$DryRun
Write-AiSessionLog ('Documents backup: ' + $DocBackup) $OrchestratorLog

# Detect the REAL vJoy device -> JOY enumeration index mapping BEFORE any collector
# grabs a device. Skip on incremental -OnlyWorkerId: probing while sibling
# collectors own devices 2-5 can pulse/re-save a partial map, and restarting
# worker 0 must not disturb the already-running cars.
if (-not $HasOnlyWorkerId) {
    $mappingPath = Join-Path $RepoRoot 'outputs\diagnostics\vjoy_mapping_latest.json'
    $reuseMap = $SkipVjoyIndexProbe -and (Test-Path -LiteralPath $mappingPath)
    if ($reuseMap) {
        Write-AiSessionLog 'vJoy device -> JOY index mapping reused (skip probe)' $OrchestratorLog
    } else {
        Invoke-DetectVjoyIndexMapping -RepoRoot $RepoRoot -SessionStamp $SessionStamp -DryRun:$DryRun
        Write-AiSessionLog 'vJoy device -> JOY index mapping detected/persisted' $OrchestratorLog
    }
} else {
    Write-AiSessionLog 'vJoy probe skipped (incremental -OnlyWorkerId; keep saved mapping)' $OrchestratorLog
}

# Wipe last session's per-worker controls snapshots so the diagnostic can never
# show stale JOY-index proof from a previous run.
# IMPORTANT: C4D launches workers one-by-one (-OnlyWorkerId per worker after W0).
# Clearing here on every incremental launch would DELETE prior workers' snapshots
# and leave only the last worker with proof — exactly the W5-only bug. Clear only
# on a full-session bootstrap (worker 0 via -Workers 1, or -Workers N all at once).
if (-not $HasOnlyWorkerId) {
    Invoke-ClearControlsSnapshots -RepoRoot $RepoRoot -DryRun:$DryRun
    Write-AiSessionLog 'controls snapshots cleared (full-session bootstrap)' $OrchestratorLog
} else {
    Write-AiSessionLog (
        'controls snapshots preserved (incremental -OnlyWorkerId worker(s) ' + ($workerIds -join ',') + ')'
    ) $OrchestratorLog
}

if ($Windowed -and $RenderPreset -eq 'SixInstances' -and -not $DryRun) {
    $aiReduced = Apply-AcAiProfileVideoSixInstances -RepoRoot $RepoRoot -BackupRoot $DocBackup
    Write-AiSessionLog ('AI profile SixInstances: ' + ($aiReduced -join ', ')) $OrchestratorLog
}

if (-not $DryRun) {
    if (-not (Test-Path -LiteralPath (Join-Path $AiRoot 'AI_INSTANCE.txt'))) {
        throw 'Copie AI invalide.'
    }
    $pluginMain = Join-Path $AiRoot 'apps\python\sensors_par\sensors_par.py'
    $skipPluginInstall = $HasOnlyWorkerId -and (Test-Path -LiteralPath $pluginMain)
    if ($skipPluginInstall) {
        Write-AiSessionLog (
            'sensors_par deja present (worker incremental), installation plugin ignoree'
        ) $OrchestratorLog
    } else {
        Invoke-RequiredPsScript -Path (Join-Path $ScriptDir 'Install-AcGymPlugin-AI.ps1') `
            -Parameters @{ RepoRoot = $RepoRoot; SkipBackup = $true; SkipIfUpToDate = $true } `
            -ErrorMessage 'Install-AcGymPlugin-AI echoue.'
        Write-AiSessionLog 'sensors_par installe/verifie (hash) avant boucle workers' $OrchestratorLog
    }
} else {
    Write-Host '[DRY-RUN] Install-AcGymPlugin-AI une fois (SkipIfUpToDate)'
}

$transitionJob = $null
$transitionServerInfo = $null
$transitionServerLauncherPid = $null
$transitionStartedHere = $false
$learnerJob = $null
if (-not $DryRun) {
    $existingManifest = Read-AiSessionManifest -RepoRoot $RepoRoot
    $transitionServerLauncherPid = Get-AiObjectPropertyOrNull -Object $existingManifest -Name 'transitionServerLauncherPid'
    $env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
    $hcArgs = @('-m', 'ac_workers.transition_server_health', '--check', '--json', '--repo-root', $RepoRoot)
    $hc = Invoke-PythonCapturedProcess -PythonExe $VenvPython -ArgumentList $hcArgs `
        -WorkingDirectory $RepoRoot -TimeoutSec 30
    $jsonLine = ($hc.Stdout -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
    $transitionServerInfo = $null
    if ($jsonLine) {
        try { $transitionServerInfo = $jsonLine | ConvertFrom-Json } catch { }
    }
    if ($transitionServerInfo -and $transitionServerInfo.ok) {
        Write-Host ('transition server reused pid=' + $transitionServerInfo.pid)
        $transitionServerInfo | Add-Member -NotePropertyName action -NotePropertyValue 'reused' -Force
        $transitionJob = Get-Process -Id $transitionServerInfo.pid -ErrorAction SilentlyContinue
        if (-not $transitionJob) {
            Write-AiSessionLog ('WARN transition server listener pid=' + $transitionServerInfo.pid + ' process handle absent') $OrchestratorLog
        }
        $repairArgs = @('-m', 'ac_workers.transition_server_health', '--verify', '--repair-manifest', '--json')
        $repairHc = Invoke-PythonCapturedProcess -PythonExe $VenvPython -ArgumentList $repairArgs `
            -WorkingDirectory $RepoRoot -TimeoutSec 15
        $repairLine = ($repairHc.Stdout -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
        if ($repairLine) {
            try {
                $transitionServerInfo = $repairLine | ConvertFrom-Json
            } catch { }
        }
    } else {
        if ($transitionServerInfo -and $transitionServerInfo.listenerPresent -and -not $transitionServerInfo.projectOwned) {
            throw ('Transition server port 24050 owned by foreign process pid=' + $transitionServerInfo.owningPid)
        }
        $tsArgs = @('-m', 'ac_workers.transition_server_main')
        $transitionJob = Start-AiRedirectedProcess -FilePath $VenvPython -ArgumentList $tsArgs `
            -WorkingDirectory $RepoRoot -Hidden `
            -StdoutLogPath $TransitionStdoutLog -StderrLogPath $TransitionStderrLog
        $transitionStartedHere = $true
        Write-AiSessionLog ('transition server starting pid=' + $transitionJob.Id) $OrchestratorLog
        $waitArgs = @('-m', 'ac_workers.transition_server_health', '--wait-ping', '--json', '--timeout', '15')
        $waitHc = Invoke-PythonCapturedProcess -PythonExe $VenvPython -ArgumentList $waitArgs `
            -WorkingDirectory $RepoRoot -TimeoutSec 30
        $waitLine = ($waitHc.Stdout -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
        if ($waitHc.ExitCode -ne 0 -or -not $waitLine) {
            throw ('Transition server not responding after start exit=' + $waitHc.ExitCode)
        }
        $transitionServerInfo = $waitLine | ConvertFrom-Json
        if (-not $transitionServerInfo.ok) {
            throw ('Transition server health: ' + $transitionServerInfo.error)
        }
        $launcherPid = $transitionJob.Id
        $transitionServerLauncherPid = $launcherPid
        if ($transitionServerInfo.pid -and [int]$transitionServerInfo.pid -ne $launcherPid) {
            Write-AiSessionLog (
                'transition server listener pid=' + $transitionServerInfo.pid `
                + ' launcher pid=' + $launcherPid
            ) $OrchestratorLog
        } elseif (-not $transitionServerInfo.pid) {
            $transitionServerInfo | Add-Member -NotePropertyName pid -NotePropertyValue $launcherPid -Force
        }
        $transitionServerInfo | Add-Member -NotePropertyName action -NotePropertyValue 'started' -Force
        $transitionServerInfo | Add-Member -NotePropertyName started_at -NotePropertyValue (Get-Date).ToUniversalTime().ToString('o') -Force
        $repairAfterStart = @('-m', 'ac_workers.transition_server_health', '--verify', '--repair-manifest', '--json')
        $repairStart = Invoke-PythonCapturedProcess -PythonExe $VenvPython -ArgumentList $repairAfterStart -WorkingDirectory $RepoRoot -TimeoutSec 15
        $repairStartLine = ($repairStart.Stdout -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
        if ($repairStartLine) {
            try { $transitionServerInfo = $repairStartLine | ConvertFrom-Json } catch { }
        }
        $transitionServerInfo | Add-Member -NotePropertyName transitionServerLauncherPid -NotePropertyValue $transitionServerLauncherPid -Force
    }
    Write-Host ('transition server ready pid=' + $transitionServerInfo.pid)
    Write-AiSessionLog (
        'transition server pid=' + $transitionServerInfo.pid `
        + ' action=' + $transitionServerInfo.action
    ) $OrchestratorLog
    if ($StartLearner) {
        $learnerArgs = @('-m', 'ac_workers.multi_sac_trainer')
        $LearnerStdoutLog = Join-Path $LogDir ('learner_' + $SessionStamp + '_stdout.log')
        $LearnerStderrLog = Join-Path $LogDir ('learner_' + $SessionStamp + '_stderr.log')
        $learnerJob = Start-AiRedirectedProcess -FilePath $VenvPython -ArgumentList $learnerArgs `
            -WorkingDirectory $RepoRoot -Hidden `
            -StdoutLogPath $LearnerStdoutLog -StderrLogPath $LearnerStderrLog
        Write-AiSessionLog ('learner processId=' + $learnerJob.Id + ' (StartLearner)') $OrchestratorLog
    }
} else {
    Write-Host '[DRY-RUN] start transition server (no learner unless -StartLearner)'
    if ($StartLearner) { Write-Host '[DRY-RUN] would start multi_sac_trainer' }
}

$manifestWorkers = @()
$startedProcs = @()
$workerFailures = @()
$launcherReadyWorkers = @()

foreach ($w in $workerIds) {
    $colProc = $null
    $acsProc = $null
    $CollectorStdoutLog = $null
    $CollectorStderrLog = $null
    $WorkerLog = Join-Path $LogDir ('worker_' + $w + '_' + $SessionStamp + '.log')
    $workerCell = $null
    $videoMeta = $null
    $launchTimings = [ordered]@{
        profilePrepMs = $null
        cmGoMs        = $null
        acsStartMs    = $null
        windowMs      = $null
        driveMs       = $null
        mgmtPortMs    = $null
        staticInfoMs  = $null
        totalMs       = $null
    }
    $workerSw = [System.Diagnostics.Stopwatch]::StartNew()
    $driveInputDelivered = $false
    $earlyColInfo = $null
    if ($Windowed -and $windowGrid) {
        $matchingCells = @($windowGrid.cells | Where-Object { $_.workerId -eq $w })
        if ($matchingCells.Count -gt 0) {
            $workerCell = $matchingCells[0]
        } else {
            $workerCell = $null
        }
    }
    try {
        Write-AiSessionLog ('--- worker ' + $w + ' ---') $OrchestratorLog
        Write-AiSessionLog ('worker ' + $w + ' log=' + $WorkerLog) $OrchestratorLog

        $profCheck = Test-WorkerProfileValid -WorkerId $w -RepoRoot $RepoRoot
        if (-not $profCheck.Ok) {
            if ($DryRun) {
                Write-Host ('[DRY-RUN] skip worker ' + $w + ': ' + $profCheck.Error)
                continue
            }
            throw $profCheck.Error
        }

        $docsRedirect = $null
        if ($DryRun) {
            $workerDocs = $Docs
        } else {
            $docsRedirect = Redirect-AcDocumentsToWorker -WorkerId $w -RepoRoot $RepoRoot -Docs $Docs -LogPath $WorkerLog
            $workerDocs = $docsRedirect.WorkerDocsRoot
        }
        $prepSw = [System.Diagnostics.Stopwatch]::StartNew()
        Apply-WorkerProfileToDocuments -WorkerId $w -Docs $workerDocs -RepoRoot $RepoRoot `
            -DryRun:$DryRun -OnboardCamera:$OnboardCamera
        $launchTimings.profilePrepMs = $prepSw.ElapsedMilliseconds

        if ($Windowed -and $workerCell) {
            if ($DryRun) {
                Write-Host (
                    '[DRY-RUN] video.ini worker ' + $w + ' windowed render ' `
                    + $workerCell.renderWidth + 'x' + $workerCell.renderHeight
                )
            } else {
                Backup-AcWorkerVideoIni -Docs $workerDocs -WorkerId $w -BackupRoot $DocBackup | Out-Null
                $videoMeta = Apply-AcWorkerVideoIniWindowed -Docs $workerDocs -Cell $workerCell `
                    -MonitorIndex $MonitorIndex -RenderPreset $RenderPreset -LogPath $WorkerLog
            }
        }

        if ($PresentationMode) {
            if ($DryRun) {
                Write-Host '[DRY-RUN] presentation: neutralize look + plugin_direct python.ini'
            } else {
                Set-AcPresentationCameraLookNeutral -Docs $workerDocs -RepoRoot $RepoRoot -WorkerId $w -LogPath $WorkerLog
                Apply-AI-Presentation-CameraConfig -Docs $workerDocs -RepoRoot $RepoRoot `
                    -PresentationMode -PresentationCamera $PresentationCamera `
                    -PresentationChaseIndex $PresentationChaseIndex | Out-Null
            }
        }

        $acsProc = $null
        $mgmtPort = Get-WorkerManagementPort -WorkerId $w
        if ($DryRun) {
            Write-Host ('[DRY-RUN] worker ' + $w + ': acs.exe + Drive (icone volant, meme sequence 0-5)')
        } else {
            $earlyColInfo = Start-WorkerCollectorProcess -WorkerId $w -RepoRoot $RepoRoot `
                -SessionStamp $SessionStamp -LogDir $LogDir -OrchestratorLog $OrchestratorLog `
                -VenvPython $VenvPython
            [void](Wait-CollectorVjoyPrehold -WorkerId $w -RepoRoot $RepoRoot -SessionId $SessionStamp `
                -CollectorPid $earlyColInfo.collectorPid -TimeoutSec 90)
            $acsSw = [System.Diagnostics.Stopwatch]::StartNew()
            $acsProc = Start-AcWorkerProcess -WorkerId $w -AiRoot $AiRoot -RepoRoot $RepoRoot `
                -LogPath $WorkerLog -PresentationMode:$PresentationMode `
                -PresentationCamera $PresentationCamera -PresentationChaseIndex $PresentationChaseIndex
            $launchTimings.acsStartMs = $acsSw.ElapsedMilliseconds
            if ($Windowed -and $workerCell) {
                if (-not (Invoke-AcWorkerWindowPlacement -Process $acsProc -Cell $workerCell -LogPath $WorkerLog)) {
                    Write-AiSessionLog ('WARN worker ' + $w + ' window placement skipped (non-fatal)') $OrchestratorLog
                }
            }
            $driveSw = [System.Diagnostics.Stopwatch]::StartNew()
            $driveOk = Invoke-AcDriveActivation -Process $acsProc -LogPath $WorkerLog `
                -MgmtPort $mgmtPort -PresentationMode:$PresentationMode -RepoRoot $RepoRoot
            $launchTimings.driveMs = $driveSw.ElapsedMilliseconds
            if (-not $driveOk) {
                throw ('Worker ' + $w + ': Drive input not delivered.')
            }
            $driveInputDelivered = $true
            if (-not (Test-TcpPortOpen -Port $mgmtPort)) {
                Write-AiSessionLog 'management port pending after Drive (collector already preheld vJoy)' $WorkerLog
            }
            if ($Windowed -and $workerCell) {
                if (-not (Invoke-AcWorkerWindowPlacement -Process $acsProc -Cell $workerCell -LogPath $WorkerLog -AfterDrive)) {
                    Write-AiSessionLog ('WARN worker ' + $w + ' window replacement after Drive skipped (non-fatal)') $OrchestratorLog
                }
            }
        }

        if (-not $DryRun -and $acsProc) {
            $ready = Wait-AcWorkerLauncherReady -Process $acsProc -MgmtPort $mgmtPort -WorkerId $w `
                -RepoRoot $RepoRoot -LogPath $WorkerLog -PollMs 200 -AllowPendingManagement
            if ($ready.TimingsMs.windowMs) { $launchTimings.windowMs = $ready.TimingsMs.windowMs }
            if ($ready.TimingsMs.mgmtPortMs) { $launchTimings.mgmtPortMs = $ready.TimingsMs.mgmtPortMs }
            if ($ready.TimingsMs.staticInfoMs) { $launchTimings.staticInfoMs = $ready.TimingsMs.staticInfoMs }
            if (-not $ready.Ok) {
                $st = $ready.State
                throw (
                    'Worker ' + $w + ' timeout: process=' + $st.processAlive `
                    + ' window=' + $st.windowReady `
                    + ' management' + $st.mgmtPort + '=' + $st.managementPortOpen `
                    + ' staticInfo=' + $st.staticInfoValid `
                    + ' elapsedMs=' + $st.elapsedMs `
                    + ' lastError=' + $st.lastError
                )
            }
            $launchTimings.totalMs = $workerSw.ElapsedMilliseconds
            Write-AcWorkerLaunchTimings -WorkerId $w -Timings $launchTimings -LogPath $WorkerLog -OrchestratorLog $OrchestratorLog
            $startedProcs += $acsProc
            $launcherReadyWorkers += [ordered]@{
                workerId            = $w
                acProc              = $acsProc
                mgmtPort            = $mgmtPort
                logPath             = $WorkerLog
                driveInputDelivered = $driveInputDelivered
                collectorLauncherPid = if ($earlyColInfo) { $earlyColInfo.collectorLauncherPid } else { $null }
                collectorPid         = if ($earlyColInfo) { $earlyColInfo.collectorPid } else { $null }
                collectorStdoutLog   = if ($earlyColInfo) { $earlyColInfo.collectorStdoutLog } else { $null }
                collectorStderrLog   = if ($earlyColInfo) { $earlyColInfo.collectorStderrLog } else { $null }
                colProc              = if ($earlyColInfo) { $earlyColInfo.colProc } else { $null }
            }
        } else {
            Write-Host ('[DRY-RUN] launcher ready gate (pid/window/port/static) worker ' + $w)
        }

        $ports = Get-WorkerPortMap -WorkerId $w
        $ports = @{ ego = $ports.Ego; opponents = $ports.Opp; mgmt = $ports.Mgmt; vjoy = $ports.VJoy }
        $winInfo = $null
        if ($Windowed -and $workerCell) {
            $winInfo = [ordered]@{
                renderWidth   = $workerCell.renderWidth
                renderHeight  = $workerCell.renderHeight
                outerX        = $workerCell.outerX
                outerY        = $workerCell.outerY
                outerWidth    = $workerCell.outerWidth
                outerHeight   = $workerCell.outerHeight
                screenX       = $workerCell.screenX
                screenY       = $workerCell.screenY
                screenWidth   = $workerCell.screenWidth
                screenHeight  = $workerCell.screenHeight
                monitorIndex  = $MonitorIndex
                fullscreen    = $false
                fpsLimit      = if ($videoMeta -and $videoMeta.FpsCap30) { 30 } else { $null }
                renderPreset  = $RenderPreset
            }
        }
        $manifestWorkers += [ordered]@{
            workerId             = $w
            acPid                = if ($acsProc) { $acsProc.Id } else { $null }
            collectorPid         = if ($earlyColInfo) { $earlyColInfo.collectorPid } else { $null }
            collectorLauncherPid = if ($earlyColInfo) { $earlyColInfo.collectorLauncherPid } else { $null }
            ports                = $ports
            vjoyDeviceId         = $ports.vjoy
            logPath              = ('ac_workers/logs/worker_' + $w + '_' + $SessionStamp + '.log')
            collectorStdoutLog   = if ($earlyColInfo) { $earlyColInfo.collectorStdoutLog } else { $null }
            collectorStderrLog   = if ($earlyColInfo) { $earlyColInfo.collectorStderrLog } else { $null }
            lockPath             = ('ac_workers/locks/worker_' + $w + '.lock')
            lastTelemetryMessage = $null
            state                = if ($DryRun) { 'dry-run' } else { 'launcher-ready' }
            window               = $winInfo
            launchTimings        = if ($DryRun) { $null } else { $launchTimings }
        }
    } catch {
        $msg = $_.Exception.Message
        if ($acsProc -and -not $acsProc.HasExited) {
            try {
                $acsProc.Kill()
                $acsProc.WaitForExit(5000)
                Write-AiSessionLog ('worker ' + $w + ' acs.exe stopped after failure') $WorkerLog
            } catch {
                Write-AiSessionLog ('worker ' + $w + ' WARN: could not stop acs.exe after failure') $WorkerLog
            }
        }
        Write-AiSessionLog ('worker ' + $w + ' ECHEC: ' + $msg) $OrchestratorLog
        Write-AiSessionLog ('worker ' + $w + ' ECHEC: ' + $msg) $WorkerLog
        $workerFailures += $w
        Write-Host ('ECHEC worker ' + $w + ': ' + $msg)
        Write-Host ('Log worker: ' + $WorkerLog)
        if ($w -eq 0 -and -not $HasOnlyWorkerId) { throw }
        continue
    } finally {
        if ($docsRedirect) { Restore-AcDocumentsRedirect -Redirect $docsRedirect }
    }
}

if (-not $DryRun) {
    foreach ($lr in @($launcherReadyWorkers)) {
        $w = $lr.workerId
        $WorkerLog = $lr.logPath
        try {
            $collectorLauncherPid = [int]($lr.collectorLauncherPid)
            $realCollectorPid = [int]($lr.collectorPid)
            $CollectorStdoutLog = $lr.collectorStdoutLog
            $CollectorStderrLog = $lr.collectorStderrLog
            $colProc = $lr.colProc
            if ($realCollectorPid -le 0 -or $collectorLauncherPid -le 0) {
                Write-AiSessionLog ('--- collector worker ' + $w + ' ---') $OrchestratorLog
                Archive-WorkerStatusBeforeCollector -WorkerId $w -RepoRoot $RepoRoot -SessionStamp $SessionStamp
                $env:AC_AI_SESSION_ID = $SessionStamp
                if ($lr.driveInputDelivered) {
                    $env:AC_AI_DRIVE_INPUT_DELIVERED = '1'
                } else {
                    $env:AC_AI_DRIVE_INPUT_DELIVERED = ''
                }
                $colArgs = @('-m', 'ac_workers.worker_collector', '--worker-id', "$w")
                $env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
                $CollectorStdoutLog = Join-Path $LogDir ('worker_' + $w + '_' + $SessionStamp + '_stdout.log')
                $CollectorStderrLog = Join-Path $LogDir ('worker_' + $w + '_' + $SessionStamp + '_stderr.log')
                $env:AC_WORKER_STDERR_LOG = $CollectorStderrLog
                $env:AC_WORKER_STDOUT_LOG = $CollectorStdoutLog
                $colProc = Start-AiRedirectedProcess -FilePath $VenvPython -ArgumentList $colArgs `
                    -WorkingDirectory $RepoRoot -Hidden `
                    -StdoutLogPath ($CollectorStdoutLog + '.launcher') -StderrLogPath ($CollectorStderrLog + '.launcher')
                Write-AiSessionLog ('collector worker ' + $w + ' launcherPid=' + $colProc.Id) $OrchestratorLog
                Assert-CollectorProcessAlive -Process $colProc -WorkerId $w `
                    -StdoutLog $CollectorStdoutLog -StderrLog $CollectorStderrLog -RepoRoot $RepoRoot
                $collectorLauncherPid = [int]$colProc.Id
                $realPidInfo = Resolve-CollectorRealPid -WorkerId $w -RepoRoot $RepoRoot `
                    -SessionId $SessionStamp -LauncherPid $collectorLauncherPid
                $realCollectorPid = [int]$realPidInfo.collectorPid
                Write-AiSessionLog ('collector worker ' + $w + ' realPid=' + $realCollectorPid) $OrchestratorLog
            } else {
                Write-AiSessionLog ('collector worker ' + $w + ' already running pid=' + $realCollectorPid) $OrchestratorLog
            }
            $requireTransition = ($RequireCollectorReady -and $w -eq 0)
            if ($requireTransition) {
                Wait-RequireCollectorReady -WorkerId $w -RepoRoot $RepoRoot -SessionId $SessionStamp `
                    -CollectorPid $realCollectorPid -LauncherPid $collectorLauncherPid -Process $colProc `
                    -StdoutLog $CollectorStdoutLog -StderrLog $CollectorStderrLog
            } elseif (-not (Invoke-WorkerHandshake -WorkerId $w -RepoRoot $RepoRoot `
                    -SessionId $SessionStamp -CollectorPid $realCollectorPid)) {
                throw ('Worker ' + $w + ': collector handshake echoue.')
            }
            foreach ($mw in @($manifestWorkers)) {
                if ([int]$mw.workerId -eq $w) {
                    $mw.collectorLauncherPid = $collectorLauncherPid
                    $mw.collectorPid = $realCollectorPid
                    $mw.collectorStdoutLog = ('ac_workers/logs/worker_' + $w + '_' + $SessionStamp + '_stdout.log')
                    $mw.collectorStderrLog = ('ac_workers/logs/worker_' + $w + '_' + $SessionStamp + '_stderr.log')
                    $mw.diagPort = (Get-WorkerDiagPort -WorkerId $w)
                    $mw.state = 'running'
                }
            }
        } catch {
            $msg = $_.Exception.Message
            Write-AiSessionLog ('collector worker ' + $w + ' ECHEC: ' + $msg) $OrchestratorLog
            $workerFailures += $w
            Write-Host ('ECHEC collector worker ' + $w + ': ' + $msg)
            if ($w -eq 0 -and -not $HasOnlyWorkerId) { throw }
        }
    }
} else {
    foreach ($w in $workerIds) {
        Write-Host ('[DRY-RUN] collector + transition apres launcher-ready worker ' + $w)
    }
}

if ($DryRun) {
    Write-Host 'Start-Multi-AI-Session DryRun: OK'
    exit 0
}

$listenerPid = if ($transitionServerInfo) { Get-AiObjectPropertyOrNull -Object $transitionServerInfo -Name 'pid' } else { $null }
if (-not $listenerPid -and $transitionJob) { $listenerPid = $transitionJob.Id }
$manifest = [ordered]@{
    schemaVersion        = 1
    sessionId            = $SessionStamp
    startedAtUtc         = (Get-Date).ToUniversalTime().ToString('o')
    aiInstallPath        = $AiRoot
    transitionServerPid          = $listenerPid
    transitionServerLauncherPid  = $transitionServerLauncherPid
    transitionServerPort       = if ($transitionServerInfo) { Get-AiObjectPropertyOrNull -Object $transitionServerInfo -Name 'port' } else { 24050 }
    transitionServerStartedAt  = if ($transitionServerInfo) { Get-AiObjectPropertyOrNull -Object $transitionServerInfo -Name 'started_at' } else { $null }
    transitionServerCommandLine = if ($transitionServerInfo) { Get-AiObjectPropertyOrNull -Object $transitionServerInfo -Name 'commandLine' } else { $null }
    transitionServerHealthy    = if ($transitionServerInfo) { [bool](Get-AiObjectPropertyOrNull -Object $transitionServerInfo -Name 'healthy') } else { $false }
    learnerPid           = if ($learnerJob) { $learnerJob.Id } else { $null }
    orchestratorLog      = $OrchestratorLog
    transitionStdoutLog  = ('ac_workers/logs/transition_' + $SessionStamp + '_stdout.log')
    transitionStderrLog  = ('ac_workers/logs/transition_' + $SessionStamp + '_stderr.log')
    learnerStdoutLog     = if ($learnerJob) { ('ac_workers/logs/learner_' + $SessionStamp + '_stdout.log') } else { $null }
    learnerStderrLog     = if ($learnerJob) { ('ac_workers/logs/learner_' + $SessionStamp + '_stderr.log') } else { $null }
    windowed          = [bool]$Windowed
    windowLayout      = if ($Windowed) { $WindowLayout } else { $null }
    monitorIndex      = if ($Windowed) { $MonitorIndex } else { $null }
    renderPreset      = if ($Windowed) { $RenderPreset } else { $null }
    workers           = @($manifestWorkers)
}
if ($HasOnlyWorkerId) {
    $previous = Read-AiSessionManifest -RepoRoot $RepoRoot
    $manifest.workers = @(Merge-ManifestWorkerEntries -ExistingWorkers $previous.workers -NewWorkers $manifestWorkers)
}
Write-AiSessionManifest -RepoRoot $RepoRoot -Manifest $manifest

if ($PresentationMode) {
    Write-AiSessionLog 'PresentationMode: camera ThirdPerson via plugin acUpdate (no F1)' $OrchestratorLog
    Hide-SessionConsoleInPresentationMode
}

if ($workerFailures.Count -gt 0) {
    Write-Host ('[WARN] Workers en echec: ' + ($workerFailures -join ', ') + '. Worker 0 conserve si actif.')
} else {
    Write-Host '[OK] Multi-AI session demarree. Status: .\scripts\Get-Multi-AI-Status.ps1'
}

if ($workerFailures.Count -gt 0 -and ($startedProcs.Count -eq 0)) {
    if ($transitionStartedHere -and $transitionJob) {
        Stop-Process -Id $transitionJob.Id -Force -ErrorAction SilentlyContinue
    }
    if ($learnerJob) {
        Stop-Process -Id $learnerJob.Id -Force -ErrorAction SilentlyContinue
    }
    throw ('Tous les workers ont echoue: ' + ($workerFailures -join ', '))
}
