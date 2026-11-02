#Requires -Version 5.1
Set-StrictMode -Version Latest

$script:AcAiSessionCommonRoot = $PSScriptRoot
$script:AcSendInputLoaded = $false
$script:AcUiAutomationLoaded = $false
$script:AcUiMouseLoaded = $false
$script:AcWindowPlacementNativeReady = $false

function Write-AiSessionLog {
    param(
        [string]$Message,
        [string]$LogPath
    )
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = [string]$ts + ' ' + [string]$Message
    Write-Host $line
    if (-not $LogPath) { return }
    $parent = Split-Path -Parent $LogPath
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        try {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        } catch {
            Write-Host ('WARN log mkdir failed: ' + $_.Exception.Message)
            return
        }
    }
    $utf8 = New-Object System.Text.UTF8Encoding $false
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            $fs = [System.IO.File]::Open(
                $LogPath,
                [System.IO.FileMode]::Append,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::ReadWrite)
            try {
                $sw = New-Object System.IO.StreamWriter($fs, $utf8)
                $sw.WriteLine($line)
                $sw.Flush()
                $sw.Dispose()
            } finally {
                $fs.Dispose()
            }
            return
        } catch {
            if ($attempt -lt 5) {
                Start-Sleep -Milliseconds (40 * $attempt)
            } else {
                Write-Host ('WARN log write failed: ' + $_.Exception.Message)
            }
        }
    }
}

function Start-AiRedirectedProcess {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory,
        [Parameter(Mandatory)][string]$StdoutLogPath,
        [Parameter(Mandatory)][string]$StderrLogPath,
        [switch]$Hidden
    )
    $stdout = [System.IO.Path]::GetFullPath($StdoutLogPath)
    $stderr = [System.IO.Path]::GetFullPath($StderrLogPath)
    if ($stdout -eq $stderr) {
        throw 'Start-AiRedirectedProcess: stdout and stderr log paths must differ'
    }
    foreach ($p in @($stdout, $stderr)) {
        $parent = Split-Path -Parent $p
        if ($parent -and -not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
    }
    $argLine = ''
    if ($ArgumentList -and $ArgumentList.Count -gt 0) {
        $argLine = Format-PythonProcessArguments -ArgumentList $ArgumentList
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = $argLine
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    if ($WorkingDirectory) { $psi.WorkingDirectory = $WorkingDirectory }
    if ($Hidden) { $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden }

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    $process.EnableRaisingEvents = $true
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $stdoutSw = New-Object System.IO.StreamWriter($stdout, $false, $utf8)
    $stderrSw = New-Object System.IO.StreamWriter($stderr, $false, $utf8)

    $null = Register-ObjectEvent -InputObject $process -EventName OutputDataReceived -MessageData $stdoutSw -Action {
        if ($EventArgs.Data -ne $null) {
            $Event.MessageData.WriteLine($EventArgs.Data)
            $Event.MessageData.Flush()
        }
    }
    $null = Register-ObjectEvent -InputObject $process -EventName ErrorDataReceived -MessageData $stderrSw -Action {
        if ($EventArgs.Data -ne $null) {
            $Event.MessageData.WriteLine($EventArgs.Data)
            $Event.MessageData.Flush()
        }
    }
    $null = Register-ObjectEvent -InputObject $process -EventName Exited -MessageData @{
        StdoutWriter = $stdoutSw
        StderrWriter = $stderrSw
    } -Action {
        try {
            $Event.MessageData.StdoutWriter.Flush()
            $Event.MessageData.StdoutWriter.Dispose()
        } catch { }
        try {
            $Event.MessageData.StderrWriter.Flush()
            $Event.MessageData.StderrWriter.Dispose()
        } catch { }
    }

    [void]$process.Start()
    $process.BeginOutputReadLine()
    $process.BeginErrorReadLine()
    return $process
}

function Format-PythonProcessArguments {
    param([string[]]$ArgumentList)
    ($ArgumentList | ForEach-Object {
        $a = [string]$_
        if ($a -match '\s|"') {
            '"' + ($a -replace '"', '""') + '"'
        } else {
            $a
        }
    }) -join ' '
}

function Invoke-PythonCapturedProcess {
    param(
        [Parameter(Mandatory)][string]$PythonExe,
        [string[]]$ArgumentList = @(),
        [string]$WorkingDirectory,
        [int]$TimeoutSec = 120
    )
    $argLine = Format-PythonProcessArguments -ArgumentList $ArgumentList
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $PythonExe
    $psi.Arguments = $argLine
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    if ($WorkingDirectory) { $psi.WorkingDirectory = $WorkingDirectory }
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $done = $proc.WaitForExit($TimeoutSec * 1000)
    if (-not $done) {
        try { $proc.Kill() } catch { }
    }
    $code = if ($proc.HasExited) { $proc.ExitCode } else { 1 }
    return [pscustomobject]@{
        ExitCode = $code
        Stdout   = $stdout
        Stderr   = $stderr
    }
}

function Get-LearnerStatusSnapshot {
    param(
        [string]$Path,
        [string]$ExpectedSessionId = ''
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $s = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        if ($ExpectedSessionId -and [string]$s.session_id -ne $ExpectedSessionId) {
            return $null
        }
        return $s
    }
    catch {
        return $null
    }
}

function Test-AiProcessRunning {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return $false }
    try {
        $null = Get-Process -Id $ProcessId -ErrorAction Stop
        return $true
    }
    catch {
        return $false
    }
}

function Resolve-RealLearnerPidFromStatus {
    param(
        [string]$StatusPath,
        [string]$SessionId
    )
    $status = Get-LearnerStatusSnapshot -Path $StatusPath -ExpectedSessionId $SessionId
    if ($null -eq $status) { return $null }
    if ($status.learner_running -ne $true) { return $null }
    $pidVal = 0
    try { $pidVal = [int]$status.learner_pid } catch { return $null }
    if ($pidVal -le 0) { return $null }
    if (-not (Test-AiProcessRunning -ProcessId $pidVal)) { return $null }
    return $pidVal
}

function Get-LearnerSupervisorProcessProbe {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return 'dead' }
    try {
        $proc = [System.Diagnostics.Process]::GetProcessById($ProcessId)
        $proc.Refresh()
        if ($proc.HasExited) { return 'dead' }
        return 'alive'
    }
    catch [System.ArgumentException] {
        return 'dead'
    }
    catch {
        return 'transient'
    }
}

function Test-LearnerStatusHeartbeatFresh {
    param(
        $Status,
        [int]$MaxAgeSec = 45
    )
    if ($null -eq $Status) { return $false }
    $hb = $Status.heartbeat
    if ($null -eq $hb) { $hb = $Status.ts }
    if ($null -eq $hb) { return $false }
    try {
        $epoch = New-Object DateTime 1970, 1, 1, 0, 0, 0, ([DateTimeKind]::Utc)
        $hbTime = $epoch.AddSeconds([double]$hb)
        $ageSec = [datetime]::UtcNow.Subtract($hbTime).TotalSeconds
        return ($ageSec -ge -2 -and $ageSec -le $MaxAgeSec)
    }
    catch {
        return $false
    }
}

function Invoke-AiEmergencyStopSubprocess {
    param([switch]$CloseGame)
    $scriptPath = Join-Path $PSScriptRoot 'Emergency-Stop-AI.ps1'
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $scriptPath)
    if ($CloseGame) { $argList += '-CloseGame' }
    $argLine = Format-PythonProcessArguments -ArgumentList $argList
    Start-Process -FilePath 'powershell.exe' -ArgumentList $argLine -Wait -WindowStyle Hidden | Out-Null
}

function Get-AiSessionManifestPath {
    param([Parameter(Mandatory)][string]$RepoRoot)
    return Join-Path $RepoRoot 'ac_workers\manifest.json'
}

function Get-AiObjectPropertyOrNull {
    param(
        [AllowNull()]$Object,
        [Parameter(Mandatory)][string]$Name
    )
    if ($null -eq $Object) { return $null }
    $prop = $Object.PSObject.Properties[$Name]
    if ($prop) { return $prop.Value }
    return $null
}

function Ensure-AiSessionManifestSchema {
    param([AllowNull()]$Manifest)
    if ($null -eq $Manifest) {
        $Manifest = [ordered]@{}
    }
    $defaults = [ordered]@{
        transitionServerPid           = $null
        transitionServerLauncherPid   = $null
        transitionServerPort          = 24050
        transitionServerHealthy       = $false
        transitionServerStartedAt     = $null
        transitionServerCommandLine   = $null
        learnerPid                    = $null
        workers                       = @()
        sessionId                     = $null
    }
    foreach ($key in @($defaults.Keys)) {
        $exists = $false
        if ($Manifest -is [hashtable] -or $Manifest -is [System.Collections.Specialized.OrderedDictionary]) {
            $exists = $Manifest.Contains($key)
            if (-not $exists) { $Manifest[$key] = $defaults[$key] }
        } else {
            $prop = $Manifest.PSObject.Properties[$key]
            if (-not $prop) {
                $Manifest | Add-Member -NotePropertyName $key -NotePropertyValue $defaults[$key] -Force
            }
        }
    }
    $workersVal = $null
    if ($Manifest -is [hashtable] -or $Manifest -is [System.Collections.Specialized.OrderedDictionary]) {
        $workersVal = $Manifest['workers']
        if ($null -eq $workersVal) {
            $Manifest['workers'] = @()
        } elseif ($workersVal -isnot [System.Array]) {
            $Manifest['workers'] = @($workersVal)
        }
    } else {
        $workersVal = Get-AiObjectPropertyOrNull -Object $Manifest -Name 'workers'
        if ($null -eq $workersVal) {
            $Manifest | Add-Member -NotePropertyName workers -NotePropertyValue @() -Force
        } elseif ($workersVal -isnot [System.Array]) {
            $Manifest | Add-Member -NotePropertyName workers -NotePropertyValue @($workersVal) -Force
        }
    }
    return $Manifest
}

function Read-AiSessionManifest {
    param([Parameter(Mandatory)][string]$RepoRoot)
    $path = Get-AiSessionManifestPath -RepoRoot $RepoRoot
    if (-not (Test-Path -LiteralPath $path)) {
        return Ensure-AiSessionManifestSchema -Manifest $null
    }
    $raw = Get-Content -LiteralPath $path -Raw
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return Ensure-AiSessionManifestSchema -Manifest $null
    }
    $parsed = $raw | ConvertFrom-Json
    return Ensure-AiSessionManifestSchema -Manifest $parsed
}

function Merge-ManifestWorkerEntries {
    param(
        $ExistingWorkers,
        $NewWorkers
    )
    # Each sequential launch used to replace the whole list, so stop-all only
    # saw the last worker (worker 5) and left the others running.
    $newIds = New-Object System.Collections.Generic.List[int]
    $fresh = @()
    foreach ($entry in @($NewWorkers)) {
        if ($null -eq $entry) { continue }
        $fresh += $entry
        [void]$newIds.Add([int]$entry.workerId)
    }
    $kept = @()
    foreach ($old in @($ExistingWorkers)) {
        if ($null -eq $old) { continue }
        $oldId = [int]$old.workerId
        if (-not $newIds.Contains($oldId)) { $kept += $old }
    }
    return @($kept) + @($fresh)
}

function Stop-DiscoveredAiWorkerProcesses {
    param([Parameter(Mandatory)][string]$RepoRoot)
    $stopped = New-Object 'System.Collections.Generic.HashSet[int]'
    $kill = {
        param([int]$ProcessId, [string]$Label)
        if ($ProcessId -le 0 -or $stopped.Contains($ProcessId)) { return }
        [void]$stopped.Add($ProcessId)
        $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if (-not $proc) { return }
        Write-Host ('[INFO] Arret ' + $Label + ' processId=' + $ProcessId + ' (' + $proc.ProcessName + ')')
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }.GetNewClosure()
    for ($wid = 0; $wid -lt 6; $wid++) {
        $statusPath = Join-Path $RepoRoot ('ac_workers\status\worker_' + $wid + '.json')
        if (-not (Test-Path -LiteralPath $statusPath)) { continue }
        try {
            $status = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
            $collectorPid = 0
            if ($status.PSObject.Properties['collector_pid'] -and $status.collector_pid) {
                $collectorPid = [int]$status.collector_pid
            }
            & $kill $collectorPid ('collector worker ' + $wid)
        } catch {
        }
    }
    $pythonProcs = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue)
    foreach ($proc in $pythonProcs) {
        $cmd = [string]$proc.CommandLine
        if ($cmd -match 'worker_collector') {
            & $kill ([int]$proc.ProcessId) 'collector python'
        }
    }
    foreach ($name in @('acs', 'AssettoCorsa')) {
        Get-Process -Name $name -ErrorAction SilentlyContinue | ForEach-Object {
            $exe = ''
            try { $exe = [string]$_.Path } catch { $exe = '' }
            if ($exe.ToLowerInvariant().Contains('assettocorsa-ai')) {
                & $kill ([int]$_.Id) 'fenetre AI'
            }
        }
    }
}

function Write-AiSessionManifest {
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)]$Manifest
    )
    $Manifest = Ensure-AiSessionManifestSchema -Manifest $Manifest
    $path = Get-AiSessionManifestPath -RepoRoot $RepoRoot
    $dir = Split-Path -Parent $path
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $tmp = $path + '.tmp'
    $Utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($tmp, ($Manifest | ConvertTo-Json -Depth 8), $Utf8NoBom)
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force -ErrorAction Stop
    }
    [System.IO.File]::Move($tmp, $path)
}

function Get-WorkerDiagPort {
    param([Parameter(Mandatory)][int]$WorkerId)
    return 24100 + $WorkerId
}

function Wait-CollectorDiagnosticReady {
    param(
        [Parameter(Mandatory)][int]$WorkerId,
        [int]$TimeoutSec = 10,
        [int]$PollMs = 200
    )
    $port = Get-WorkerDiagPort -WorkerId $WorkerId
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-TcpPortOpen -Port $port) { return $true }
        Start-Sleep -Milliseconds $PollMs
    }
    return $false
}

function Get-AiLogTailMultiline {
    param(
        [Parameter(Mandatory)][string]$LogPath,
        [int]$Lines = 100
    )
    if (-not (Test-Path -LiteralPath $LogPath)) { return '(fichier absent)' }
    return (Get-Content -LiteralPath $LogPath -Tail $Lines -ErrorAction SilentlyContinue) -join "`n"
}

function Assert-CollectorProcessAlive {
    param(
        [Parameter(Mandatory)]$Process,
        [Parameter(Mandatory)][int]$WorkerId,
        [Parameter(Mandatory)][string]$StdoutLog,
        [Parameter(Mandatory)][string]$StderrLog,
        [Parameter(Mandatory)][string]$RepoRoot,
        [int]$DiagTimeoutSec = 10
    )
    if (-not $Process) {
        throw ('Collector worker ' + $WorkerId + ': process null')
    }
    $deadline = (Get-Date).AddSeconds($DiagTimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Wait-CollectorDiagnosticReady -WorkerId $WorkerId -TimeoutSec 1 -PollMs 200) {
            Write-Host ('collector worker ' + $WorkerId + ' diagnostic ready port=' + (Get-WorkerDiagPort -WorkerId $WorkerId))
            return
        }
        if ($Process.HasExited) {
            if (Wait-CollectorDiagnosticReady -WorkerId $WorkerId -TimeoutSec 1 -PollMs 200) {
                Write-Host ('collector worker ' + $WorkerId + ' diagnostic ready after launcher exit port=' + (Get-WorkerDiagPort -WorkerId $WorkerId))
                return
            }
            $tailOut = Get-AiLogTailMultiline -LogPath $StdoutLog
            $tailErr = Get-AiLogTailMultiline -LogPath $StderrLog
            throw (
                'Collector worker ' + $WorkerId + ' exited before diagnostic ready. ' `
                + 'ExitCode=' + $Process.ExitCode + "`n--- stdout ---`n" + $tailOut `
                + "`n--- stderr ---`n" + $tailErr
            )
        }
        Start-Sleep -Milliseconds 200
    }
    if (Wait-CollectorDiagnosticReady -WorkerId $WorkerId -TimeoutSec 1 -PollMs 200) {
        Write-Host ('collector worker ' + $WorkerId + ' diagnostic ready port=' + (Get-WorkerDiagPort -WorkerId $WorkerId))
        return
    }
    if ($Process.HasExited) {
        $tailOut = Get-AiLogTailMultiline -LogPath $StdoutLog
        $tailErr = Get-AiLogTailMultiline -LogPath $StderrLog
        throw (
            'Collector worker ' + $WorkerId + ' exited during diagnostic wait. ' `
            + 'ExitCode=' + $Process.ExitCode + "`n--- stdout ---`n" + $tailOut `
            + "`n--- stderr ---`n" + $tailErr
        )
    }
    $tailErr = Get-AiLogTailMultiline -LogPath $StderrLog
    throw (
        'Collector worker ' + $WorkerId + ' diagnostic port ' + (Get-WorkerDiagPort -WorkerId $WorkerId) `
        + ' not open after ' + $DiagTimeoutSec + 's. stderr:`n' + $tailErr
    )
}

function Archive-WorkerStatusBeforeCollector {
    param(
        [Parameter(Mandatory)][int]$WorkerId,
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$SessionStamp
    )
    $statusPath = Join-Path $RepoRoot ('ac_workers\status\worker_' + $WorkerId + '.json')
    if (-not (Test-Path -LiteralPath $statusPath)) { return }
    $archiveDir = Join-Path $RepoRoot 'ac_workers\status\archive'
    New-Item -ItemType Directory -Path $archiveDir -Force | Out-Null
    $dest = Join-Path $archiveDir ('worker_' + $WorkerId + '_' + $SessionStamp + '.json')
    Move-Item -LiteralPath $statusPath -Destination $dest -Force
}

function Start-WorkerCollectorProcess {
    param(
        [Parameter(Mandatory)][int]$WorkerId,
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$SessionStamp,
        [Parameter(Mandatory)][string]$LogDir,
        [Parameter(Mandatory)][string]$OrchestratorLog,
        [Parameter(Mandatory)][string]$VenvPython,
        [switch]$DriveInputDelivered
    )
    Write-AiSessionLog ('--- collector worker ' + $WorkerId + ' (early) ---') $OrchestratorLog
    Archive-WorkerStatusBeforeCollector -WorkerId $WorkerId -RepoRoot $RepoRoot -SessionStamp $SessionStamp
    $env:AC_AI_SESSION_ID = $SessionStamp
    if ($DriveInputDelivered) {
        $env:AC_AI_DRIVE_INPUT_DELIVERED = '1'
    } else {
        $env:AC_AI_DRIVE_INPUT_DELIVERED = ''
    }
    $colArgs = @('-m', 'ac_workers.worker_collector', '--worker-id', "$WorkerId")
    $env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
    $CollectorStdoutLog = Join-Path $LogDir ('worker_' + $WorkerId + '_' + $SessionStamp + '_stdout.log')
    $CollectorStderrLog = Join-Path $LogDir ('worker_' + $WorkerId + '_' + $SessionStamp + '_stderr.log')
    $env:AC_WORKER_STDERR_LOG = $CollectorStderrLog
    $env:AC_WORKER_STDOUT_LOG = $CollectorStdoutLog
    $colProc = Start-AiRedirectedProcess -FilePath $VenvPython -ArgumentList $colArgs `
        -WorkingDirectory $RepoRoot -Hidden `
        -StdoutLogPath ($CollectorStdoutLog + '.launcher') -StderrLogPath ($CollectorStderrLog + '.launcher')
    Write-AiSessionLog ('collector worker ' + $WorkerId + ' launcherPid=' + $colProc.Id) $OrchestratorLog
    Assert-CollectorProcessAlive -Process $colProc -WorkerId $WorkerId `
        -StdoutLog $CollectorStdoutLog -StderrLog $CollectorStderrLog -RepoRoot $RepoRoot
    $collectorLauncherPid = [int]$colProc.Id
    $realPidInfo = Resolve-CollectorRealPid -WorkerId $WorkerId -RepoRoot $RepoRoot `
        -SessionId $SessionStamp -LauncherPid $collectorLauncherPid
    $realCollectorPid = [int]$realPidInfo.collectorPid
    Write-AiSessionLog ('collector worker ' + $WorkerId + ' realPid=' + $realCollectorPid) $OrchestratorLog
    return [ordered]@{
        colProc                = $colProc
        collectorLauncherPid   = $collectorLauncherPid
        collectorPid           = $realCollectorPid
        collectorStdoutLog     = ('ac_workers/logs/worker_' + $WorkerId + '_' + $SessionStamp + '_stdout.log')
        collectorStderrLog     = ('ac_workers/logs/worker_' + $WorkerId + '_' + $SessionStamp + '_stderr.log')
        diagPort               = (Get-WorkerDiagPort -WorkerId $WorkerId)
    }
}

function Resolve-CollectorRealPid {
    param(
        [Parameter(Mandatory)][int]$WorkerId,
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$SessionId,
        [Parameter(Mandatory)][int]$LauncherPid,
        [int]$TimeoutSec = 120
    )
    $venv = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
    $env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
    $args = @(
        '-m', 'ac_workers.collector_pid', 'resolve',
        '--worker-id', "$WorkerId",
        '--session-id', $SessionId,
        '--launcher-pid', "$LauncherPid",
        '--repo-root', $RepoRoot,
        '--timeout', "$TimeoutSec"
    )
    $hc = Invoke-PythonCapturedProcess -PythonExe $venv -ArgumentList $args -WorkingDirectory $RepoRoot -TimeoutSec ($TimeoutSec + 15)
    $line = ($hc.Stdout -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
    if ($hc.ExitCode -ne 0 -or -not $line) {
        throw ('collector_real_pid_not_reported worker ' + $WorkerId + ' launcherPid=' + $LauncherPid + ' stderr=' + ($hc.Stderr -replace '\s+', ' '))
    }
    $parsed = $line | ConvertFrom-Json
    if (-not $parsed.ok -or [int]$parsed.collector_pid -le 0) {
        $err = [string]$parsed.error
        if (-not $err) { $err = 'collector_real_pid_not_reported' }
        throw ($err + ' worker ' + $WorkerId + ' launcherPid=' + $LauncherPid)
    }
    return [ordered]@{
        collectorPid = [int]$parsed.collector_pid
        launcherPid  = $LauncherPid
    }
}

function Wait-CollectorVjoyPrehold {
    param(
        [Parameter(Mandatory)][int]$WorkerId,
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$SessionId,
        [Parameter(Mandatory)][int]$CollectorPid,
        [int]$TimeoutSec = 90
    )
    $venv = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
    $env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
    $args = @(
        '-m', 'ac_workers.collector_ready',
        '--wait-vjoy-prehold',
        '--worker-id', "$WorkerId",
        '--session-id', $SessionId,
        '--collector-pid', "$CollectorPid",
        '--repo-root', $RepoRoot,
        '--timeout', "$TimeoutSec"
    )
    $hc = Invoke-PythonCapturedProcess -PythonExe $venv -ArgumentList $args -WorkingDirectory $RepoRoot -TimeoutSec ($TimeoutSec + 15)
    foreach ($line in @($hc.Stdout -split "`n" | Where-Object { $_.Trim() })) { Write-Host $line }
    if ($hc.ExitCode -ne 0) {
        # Non-fatal: the prehold wait only gates acs.exe startup so vJoy is owned
        # first. A false negative here (status read race, etc.) must NOT kill a
        # collector that is otherwise healthy. Downstream checks (management
        # port, Drive activation) still catch real failures.
        Write-Warning ('VjoyPrehold worker ' + $WorkerId + ' not confirmed (continuing): ' + (($hc.Stdout + $hc.Stderr) -replace '\s+', ' '))
        return $false
    }
    return $true
}

function Wait-RequireCollectorReady {
    param(
        [Parameter(Mandatory)][int]$WorkerId,
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$SessionId,
        [Parameter(Mandatory)][int]$CollectorPid,
        [int]$LauncherPid = 0,
        [Parameter(Mandatory)]$Process,
        [string]$StdoutLog,
        [string]$StderrLog,
        [int]$TimeoutSec = 180
    )
    $venv = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
    $env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
    $args = @(
        '-m', 'ac_workers.collector_ready',
        '--worker-id', "$WorkerId",
        '--session-id', $SessionId,
        '--collector-pid', "$CollectorPid",
        '--launcher-pid', "$LauncherPid",
        '--repo-root', $RepoRoot,
        '--stdout-log', ($StdoutLog -as [string]),
        '--stderr-log', ($StderrLog -as [string]),
        '--timeout', "$TimeoutSec"
    )
    $invokeParams = @{
        PythonExe        = $venv
        ArgumentList     = $args
        WorkingDirectory = $RepoRoot
        TimeoutSec       = ($TimeoutSec + 15)
    }
    $hc = Invoke-PythonCapturedProcess @invokeParams
    $lines = @($hc.Stdout -split "`n" | Where-Object { $_.Trim() })
    foreach ($line in @($lines)) { Write-Host $line }
    if ($hc.ExitCode -ne 0) {
        $tailOut = Get-AiLogTailMultiline -LogPath $StdoutLog
        $tailErr = Get-AiLogTailMultiline -LogPath $StderrLog
        $alive = $true
        if ($Process -and $Process.HasExited) { $alive = $false }
        throw (
            'RequireCollectorReady worker ' + $WorkerId + ' failed launcherAlive=' + $alive `
            + ' collectorPid=' + $CollectorPid + ' launcherPid=' + $LauncherPid `
            + "`n--- stdout tail ---`n" + $tailOut + "`n--- stderr tail ---`n" + $tailErr
        )
    }
}

function Get-AiLogTail {
    param(
        [string]$LogPath,
        [int]$Lines = 5
    )
    if (-not $LogPath) { return '(absent)' }
    $full = if ([System.IO.Path]::IsPathRooted($LogPath)) { $LogPath } else { $LogPath }
    if (-not (Test-Path -LiteralPath $full)) { return '(fichier absent)' }
    return (Get-Content -LiteralPath $full -Tail $Lines -ErrorAction SilentlyContinue) -join ' | '
}

function Invoke-RequiredPsScript {
    param(
        [Parameter(Mandatory)][string]$Path,
        [hashtable]$Parameters = @{},
        [string]$ErrorMessage
    )
    & $Path @Parameters
    if (-not $?) {
        if ($ErrorMessage) { throw $ErrorMessage }
        throw ('Script echoue: ' + [string]$Path)
    }
}

function Get-CollectionCount {
    param([AllowNull()]$Items)
    if ($null -eq $Items) { return 0 }
    if ($Items -is [string]) { return 1 }
    if ($Items -isnot [System.Collections.IEnumerable]) { return 1 }
    $n = 0
    foreach ($i in $Items) { $n++ }
    return $n
}

function Get-IniValue {
    param(
        [Parameter(Mandatory)][string]$Path,
        [string]$Section = '',
        [Parameter(Mandatory)][string]$Key
    )
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $wantGlobal = [string]::IsNullOrWhiteSpace($Section)
    $inTarget = $wantGlobal
    $currentSection = ''
    foreach ($rawLine in @(Get-Content -LiteralPath $Path)) {
        $trim = $rawLine.Trim()
        if ($trim -match '^\[(.+)\]\s*$') {
            $currentSection = $Matches[1]
            $inTarget = $wantGlobal -or ($currentSection -eq $Section)
            continue
        }
        if (-not $inTarget) { continue }
        $eqIdx = $trim.IndexOf('=')
        if ($eqIdx -lt 1) { continue }
        $lineKey = $trim.Substring(0, $eqIdx).Trim()
        if ($lineKey -eq $Key) {
            return $trim.Substring($eqIdx + 1)
        }
    }
    return $null
}

function Set-IniValue {
    param(
        [Parameter(Mandatory)][string]$Path,
        [string]$Section = '',
        [Parameter(Mandatory)][string]$Key,
        [AllowNull()][string]$Value = '',
        [switch]$CreateFile
    )
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $newLine = [string]$Key + '=' + [string]$Value
    $wantGlobal = [string]::IsNullOrWhiteSpace($Section)
    $lines = @()
    if (Test-Path -LiteralPath $Path) {
        $lines = @(Get-Content -LiteralPath $Path)
    } elseif (-not $CreateFile) {
        return
    }

    $out = New-Object System.Collections.Generic.List[string]
    $inTarget = $wantGlobal
    $currentSection = ''
    $replaced = $false
    $sectionExists = $false

    foreach ($rawLine in $lines) {
        $trim = $rawLine.Trim()
        if ($trim -match '^\[(.+)\]\s*$') {
            if (-not $wantGlobal -and $inTarget -and -not $replaced) {
                $out.Add($newLine)
                $replaced = $true
            }
            $currentSection = $Matches[1]
            if ($currentSection -eq $Section) { $sectionExists = $true }
            $inTarget = $wantGlobal -or ($currentSection -eq $Section)
            $out.Add($rawLine)
            continue
        }
        if ($inTarget) {
            $eqIdx = $trim.IndexOf('=')
            if ($eqIdx -ge 1) {
                $lineKey = $trim.Substring(0, $eqIdx).Trim()
                if ($lineKey -eq $Key) {
                    $out.Add($newLine)
                    $replaced = $true
                    continue
                }
            }
        }
        $out.Add($rawLine)
    }

    if (-not $replaced) {
        if (-not $wantGlobal -and -not $sectionExists) {
            $out.Add('[' + [string]$Section + ']')
        }
        $out.Add($newLine)
    }

    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines($Path, $out, $utf8)
}

function Remove-IniSection {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Section
    )
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $out = New-Object System.Collections.Generic.List[string]
    $skip = $false
    foreach ($rawLine in @(Get-Content -LiteralPath $Path)) {
        $trim = $rawLine.Trim()
        if ($trim -match '^\[(.+)\]\s*$') {
            if ($Matches[1] -eq $Section) {
                $skip = $true
                continue
            }
            $skip = $false
        }
        if (-not $skip) { $out.Add($rawLine) }
    }
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines($Path, $out, $utf8)
}

function Ensure-IniSection {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Section
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        $parent = Split-Path -Parent $Path
        if ($parent -and -not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        $header = '[' + [string]$Section + ']'
        $utf8 = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::WriteAllText($Path, $header + "`r`n", $utf8)
        return
    }
    foreach ($rawLine in @(Get-Content -LiteralPath $Path)) {
        $trim = $rawLine.Trim()
        if ($trim -match '^\[(.+)\]\s*$' -and $Matches[1] -eq $Section) {
            return
        }
    }
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::AppendAllText($Path, "`r`n[" + [string]$Section + "]`r`n", $utf8)
}

function Write-SensorsParPythonIni {
    param(
        [string]$TargetPath,
        [int]$WorkerId = 0
    )
    Remove-IniSection -Path $TargetPath -Section 'sensors_par'
    Ensure-IniSection -Path $TargetPath -Section 'sensors_par'
    Set-IniValue -Path $TargetPath -Section 'sensors_par' -Key 'ACTIVE' -Value '1' -CreateFile
    $p = Get-WorkerPortMap -WorkerId $WorkerId
    Set-IniValue -Path $TargetPath -Section 'sensors_par' -Key 'ACGYM_WORKER_ID' -Value ([string]$WorkerId) -CreateFile
    Set-IniValue -Path $TargetPath -Section 'sensors_par' -Key 'ACGYM_EGO_PORT' -Value ([string]$p.Ego) -CreateFile
    Set-IniValue -Path $TargetPath -Section 'sensors_par' -Key 'ACGYM_OPPONENTS_PORT' -Value ([string]$p.Opp) -CreateFile
    Set-IniValue -Path $TargetPath -Section 'sensors_par' -Key 'ACGYM_MGMT_PORT' -Value ([string]$p.Mgmt) -CreateFile
    Set-IniValue -Path $TargetPath -Section 'sensors_par' -Key 'ACGYM_VJOY_DEVICE' -Value ([string]$p.VJoy) -CreateFile
}

function Write-AcSensorsParPresentationConfig {
    param(
        [Parameter(Mandatory)][string]$PythonIniPath,
        [switch]$PresentationMode,
        [ValidateSet('', 'ThirdPerson')]
        [string]$PresentationCamera = '',
        [ValidateSet(0, 1)]
        [int]$PresentationChaseIndex = 0
    )
    Write-SensorsParPythonIni -TargetPath $PythonIniPath
    if ($PresentationMode) {
        Set-IniValue -Path $PythonIniPath -Section 'sensors_par' -Key 'PRESENTATION_MODE' -Value '1' -CreateFile
        if ($PresentationCamera) {
            Set-IniValue -Path $PythonIniPath -Section 'sensors_par' -Key 'PRESENTATION_CAMERA' -Value $PresentationCamera -CreateFile
        }
        Set-IniValue -Path $PythonIniPath -Section 'sensors_par' -Key 'PRESENTATION_DRIVABLE_INDEX' -Value ([string]$PresentationChaseIndex) -CreateFile
    } else {
        Set-IniValue -Path $PythonIniPath -Section 'sensors_par' -Key 'PRESENTATION_MODE' -Value '0' -CreateFile
        Set-IniValue -Path $PythonIniPath -Section 'sensors_par' -Key 'PRESENTATION_CAMERA' -Value '' -CreateFile
        Set-IniValue -Path $PythonIniPath -Section 'sensors_par' -Key 'PRESENTATION_DRIVABLE_INDEX' -Value '0' -CreateFile
    }
}

function Set-AcRaceIniCarTrack {
    param(
        [string]$RaceIniPath,
        [string]$Car = 'lotus_exos_125',
        [string]$Track = 'monza',
        [string]$TrackConfig = ''
    )
    if (-not (Test-Path -LiteralPath $RaceIniPath)) { return }
    Set-IniValue -Path $RaceIniPath -Section 'RACE' -Key 'MODEL' -Value $Car -CreateFile
    Set-IniValue -Path $RaceIniPath -Section 'RACE' -Key 'TRACK' -Value $Track -CreateFile
    Set-IniValue -Path $RaceIniPath -Section 'RACE' -Key 'CONFIG_TRACK' -Value $TrackConfig -CreateFile
}

function Get-AcUiSettings {
    param([string]$RepoRoot)
    $path = Join-Path $RepoRoot 'config\ui_settings.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try {
        return Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Sync-WorkerRaceFromUiSettings {
    param(
        [string]$RepoRoot,
        [switch]$DryRun
    )
    $ui = Get-AcUiSettings -RepoRoot $RepoRoot
    if (-not $ui) { return $false }
    $car = [string]$ui.CarId
    $track = [string]$ui.TrackName
    $trackConfig = [string]$ui.TrackConfig
    if (-not $car) { $car = 'lotus_exos_125' }
    if (-not $track) { $track = 'monza' }
    if ($null -eq $trackConfig) { $trackConfig = '' }

    $targets = @(
        (Join-Path $RepoRoot 'ac_profiles\ai\cfg\race.ini')
    )
    $workersRoot = Join-Path $RepoRoot 'ac_profiles\workers'
    if (Test-Path -LiteralPath $workersRoot) {
        Get-ChildItem -LiteralPath $workersRoot -Directory -Filter 'worker_*' | ForEach-Object {
            $targets += (Join-Path $_.FullName 'cfg\race.ini')
        }
    }
    foreach ($raceIni in $targets) {
        if (-not (Test-Path -LiteralPath $raceIni)) { continue }
        if ($DryRun) {
            Write-Host ("[DRY-RUN] race.ini car/track -> " + $car + " / " + $track + " (" + $trackConfig + ") :: " + $raceIni)
            continue
        }
        Set-AcRaceIniCarTrack -RaceIniPath $raceIni -Car $car -Track $track -TrackConfig $trackConfig
    }
    return $true
}

function Get-AcRunningProcessNames {
    $names = @('AssettoCorsa', 'acs', 'acs_x86', 'Content Manager', 'ContentManager')
    $running = @()
    foreach ($n in $names) {
        if (Get-Process -Name $n -ErrorAction SilentlyContinue) { $running += $n }
    }
    return @($running)
}

function Stop-AcProcessesForce {
    param([string]$LogPath)
    foreach ($n in @('acs', 'acs_x86', 'AssettoCorsa', 'Content Manager', 'ContentManager')) {
        Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object {
            Write-AiSessionLog ('ForceClose: ' + $_.ProcessName + ' pid=' + $_.Id) $LogPath
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 2
}

function Test-TrainingAlreadyRunning {
    param([string]$RepoRoot)
    if (-not $RepoRoot) {
        $RepoRoot = (Resolve-Path (Join-Path $script:AcAiSessionCommonRoot '..')).Path
    }
    $repoNorm = $RepoRoot.ToLowerInvariant().TrimEnd('\')
    $venvMarker = '.venv-acgym'
    $procs = @(
        Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
    )
    foreach ($proc in $procs) {
        $cmd = $proc.CommandLine
        if (-not $cmd) { continue }
        $cmdLower = $cmd.ToLowerInvariant()
        if ($cmdLower -notmatch 'train\.py(\s|$|")') { continue }
        if ($cmdLower -notmatch [regex]::Escape($repoNorm) -and $cmdLower -notmatch [regex]::Escape($venvMarker)) { continue }
        if ($cmdLower -match 'cursor|powershell|pwsh') { continue }
        if ($cmdLower -match '\s-c\s') { continue }
        if ($cmdLower -match 'preflightonly|run-first-sac-training') { continue }
        return $true
    }
    return $false
}

function Copy-FileToProfile {
    param(
        [string]$Source,
        [string]$Destination,
        [string]$BackupRoot,
        [string]$RelativePath
    )
    if (-not (Test-Path -LiteralPath $Source)) { return $false }
    $destParent = Split-Path -Parent $Destination
    if ($destParent -and -not (Test-Path -LiteralPath $destParent)) {
        New-Item -ItemType Directory -Path $destParent -Force | Out-Null
    }
    if (Test-Path -LiteralPath $Destination) {
        $bak = Join-Path $BackupRoot $RelativePath
        $bakParent = Split-Path -Parent $bak
        if ($bakParent -and -not (Test-Path -LiteralPath $bakParent)) {
            New-Item -ItemType Directory -Path $bakParent -Force | Out-Null
        }
        Copy-Item -LiteralPath $Destination -Destination $bak -Force
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
    return $true
}

function Get-VjoyPresetFiles {
    param([string]$SavedSetupsDir)
    if (-not (Test-Path -LiteralPath $SavedSetupsDir)) { return @() }
    return @(Get-ChildItem -LiteralPath $SavedSetupsDir -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match 'vjoy' })
}

function Get-WorkerProfileRoot {
    param([int]$WorkerId, [string]$RepoRoot)
    return Join-Path $RepoRoot ('ac_profiles\workers\worker_' + $WorkerId)
}

function Get-WorkerPortMap {
    param([Parameter(Mandatory)][int]$WorkerId)
    if ($WorkerId -lt 0 -or $WorkerId -gt 5) {
        throw ('WorkerId hors plage 0-5: ' + $WorkerId)
    }
    $ego = 2345 + ($WorkerId * 10)
    return @{
        Ego  = $ego
        Opp  = $ego + 1
        Mgmt = $ego + 2
        VJoy = $WorkerId + 1
    }
}

function Get-WorkerVjoyPresetName {
    param([int]$WorkerId)
    return ('Vjoy_W' + $WorkerId + '.ini')
}

function Read-DetectedJoyIndex {
    param(
        [Parameter(Mandatory)][int]$VJoyDevice,
        [Parameter(Mandatory)][string]$RepoRoot
    )
    $mappingPath = Join-Path $RepoRoot 'outputs\diagnostics\vjoy_mapping_latest.json'
    if (Test-Path -LiteralPath $mappingPath) {
        try {
            $mapping = Get-Content -LiteralPath $mappingPath -Raw | ConvertFrom-Json
            $idx = $mapping.device_to_index."$VJoyDevice"
            if ($null -ne $idx) { return [int]$idx }
        } catch { }
    }
    throw ('JOY index introuvable pour vJoy device ' + $VJoyDevice + ' (mapping manquant). Relance la detection vJoy.')
}

function Get-AcWorkerCfgFiles {
    return @(
        'cfg\python.ini', 'cfg\controls.ini', 'cfg\race.ini', 'cfg\video.ini',
        'cfg\assists.ini', 'cfg\gameplay.ini', 'cfg\audio.ini'
    )
}

function Get-AcCameraProfileFiles {
    return @('cfg\camera_manager.ini', 'cfg\camera_onboard.ini')
}

function Get-AcPresentationCameraNamePatterns {
    return @('camera', 'chase', 'onboard', 'view', 'neck', 'look', 'glance', 'mouse', 'pov')
}

function Get-AcPresentationCameraCandidateFiles {
    param([string]$CfgRoot)
    if (-not (Test-Path -LiteralPath $CfgRoot)) { return @() }
    $patterns = Get-AcPresentationCameraNamePatterns
    $found = @{}
    foreach ($f in @(Get-ChildItem -LiteralPath $CfgRoot -Filter '*.ini' -File -ErrorAction SilentlyContinue)) {
        $nameLower = $f.Name.ToLowerInvariant()
        foreach ($pat in $patterns) {
            if ($nameLower -match [regex]::Escape($pat)) {
                $found[$f.FullName] = $true
                break
            }
        }
    }
    return @($found.Keys | Sort-Object)
}

function Get-AcIniFileSnapshot {
    param([string[]]$Files)
    $snap = @{}
    foreach ($path in $Files) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $rows = @{}
        $section = ''
        foreach ($rawLine in @(Get-Content -LiteralPath $path)) {
            $trim = $rawLine.Trim()
            if ($trim -match '^\[(.+)\]\s*$') {
                $section = $Matches[1]
                continue
            }
            if ($trim -match '^([^=;#]+)=(.*)$') {
                $key = $Matches[1].Trim()
                $val = (($Matches[2] -split ';')[0]).Trim()
                $rows[($section + '|' + $key)] = $val
            }
        }
        $snap[$path] = $rows
    }
    return $snap
}

function Compare-AcIniSnapshots {
    param($Before, $After)
    $diffs = @()
    $allPaths = @($Before.Keys + $After.Keys | Select-Object -Unique)
    foreach ($path in $allPaths) {
        $b = $Before[$path]
        $a = $After[$path]
        if (-not $b) {
            $diffs += [pscustomobject]@{ File = $path; Section = ''; Key = '(new file)'; Before = ''; After = 'created' }
            continue
        }
        if (-not $a) {
            $diffs += [pscustomobject]@{ File = $path; Section = ''; Key = '(removed)'; Before = 'existed'; After = '' }
            continue
        }
        $allKeys = @($b.Keys + $a.Keys | Select-Object -Unique)
        foreach ($k in $allKeys) {
            $bv = if ($b.ContainsKey($k)) { $b[$k] } else { '' }
            $av = if ($a.ContainsKey($k)) { $a[$k] } else { '' }
            if ($bv -ne $av) {
                $parts = $k -split '\|', 2
                $diffs += [pscustomobject]@{
                    File    = $path
                    Section = $parts[0]
                    Key     = $parts[1]
                    Before  = $bv
                    After   = $av
                }
            }
        }
    }
    return $diffs
}

function Get-AcPresentationCameraConfigPath {
    param([string]$RepoRoot)
    return Join-Path $RepoRoot 'ac_profiles\presentation_camera\config.json'
}

function Get-AcPresentationCameraDefaults {
    return @{
        Method              = 'plugin_direct'
        CameraMode          = 'Drivable'
        CameraModeId        = 2
        DrivableCameraIndex = 0
    }
}

function Get-AcPresentationCameraConfig {
    param([string]$RepoRoot)
    $defaults = Get-AcPresentationCameraDefaults
    $path = Get-AcPresentationCameraConfigPath -RepoRoot $RepoRoot
    if (-not (Test-Path -LiteralPath $path)) { return $defaults }
    try {
        $json = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        if ($json.method) { $defaults.Method = [string]$json.method }
        if ($json.cameraMode) { $defaults.CameraMode = [string]$json.cameraMode }
        if ($null -ne $json.cameraModeId) { $defaults.CameraModeId = [int]$json.cameraModeId }
        if ($null -ne $json.drivableCameraIndex) { $defaults.DrivableCameraIndex = [int]$json.drivableCameraIndex }
    } catch {
    }
    return $defaults
}

function Get-AcPresentationLookNeutralKeys {
    return @('KEY', 'JOY', 'BUTTON', 'AXLE', 'POV', 'XBOXBUTTON')
}

function Get-AcDrivingControlSections {
    return @(
        'STEER', 'THROTTLE', 'BRAKES', 'CLUTCH', 'GEARUP', 'GEARDN', 'HANDBRAKE', 'SHIFTER'
    )
}

function Get-AcCameraLookControlSections {
    return @(
        'GLANCELEFT', 'GLANCERIGHT', 'GLANCEBACK',
        'LOOKLEFT', 'LOOKRIGHT', 'LOOKBACK',
        'MOUSE_LOOK', 'MOUSE_STEERING', 'CAMERA_YAW', 'ACTION_CHANGE_CAMERA'
    )
}

function Get-AcCameraLookSectionsFromIni {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    $found = @()
    foreach ($rawLine in @(Get-Content -LiteralPath $Path)) {
        $trim = $rawLine.Trim()
        if ($trim -match '^\[(.+)\]\s*$') {
            $s = $Matches[1].ToUpperInvariant()
            if ($s -match 'GLANCE|LOOK|MOUSE_LOOK|CAMERA_YAW|POV|HAT') {
                $found += $Matches[1]
            }
        }
    }
    return @($found | Select-Object -Unique)
}

function Set-AcPresentationCameraLookNeutral {
    param(
        [string]$Docs,
        [string]$RepoRoot,
        [int]$WorkerId = -1,
        [string]$LogPath,
        [switch]$DryRun
    )
    $neutralized = @()
    $targets = @()
    $controlsLive = Join-Path $Docs 'cfg\controls.ini'
    if (Test-Path -LiteralPath $controlsLive) { $targets += $controlsLive }
    if ($WorkerId -ge 0) {
        $vjoyName = Get-WorkerVjoyPresetName -WorkerId $WorkerId
        $vjoyLive = Join-Path $Docs ('cfg\controllers\savedsetups\' + $vjoyName)
        if (Test-Path -LiteralPath $vjoyLive) { $targets += $vjoyLive }
    } else {
        $setupsDir = Join-Path $Docs 'cfg\controllers\savedsetups'
        if (Test-Path -LiteralPath $setupsDir) {
            $targets += @(Get-ChildItem -LiteralPath $setupsDir -Filter 'Vjoy*.ini' -File -ErrorAction SilentlyContinue |
                ForEach-Object { $_.FullName })
        }
    }
    $keys = Get-AcPresentationLookNeutralKeys
    foreach ($path in $targets) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $sections = @(Get-AcCameraLookControlSections) + @(Get-AcCameraLookSectionsFromIni -Path $path)
        $sections = @($sections | Select-Object -Unique)
        foreach ($section in $sections) {
            $hadBinding = $false
            foreach ($key in $keys) {
                $cur = Get-IniValue -Path $path -Section $section -Key $key
                if ($cur -and $cur -ne '-1' -and $cur -ne '') { $hadBinding = $true }
                if ($DryRun) { continue }
                Set-IniValue -Path $path -Section $section -Key $key -Value '-1' -CreateFile
            }
            if ($hadBinding) {
                $rel = $path
                if ($path.StartsWith($Docs)) {
                    $rel = $path.Substring($Docs.Length).TrimStart('\')
                }
                $neutralized += ($rel + ' [' + $section + ']')
            }
        }
    }
    foreach ($line in $neutralized) {
        Write-AiSessionLog ('look neutralized: ' + $line) $LogPath
    }
    return $neutralized
}

function Apply-AI-Presentation-CameraConfig {
    param(
        [string]$Docs,
        [string]$RepoRoot,
        [switch]$PresentationMode,
        [ValidateSet('', 'ThirdPerson')]
        [string]$PresentationCamera = '',
        [ValidateSet(0, 1)]
        [int]$PresentationChaseIndex = 0,
        [switch]$DryRun
    )
    if ($DryRun) {
        Write-Host '[DRY-RUN] Apply-AI-Presentation-CameraConfig plugin_direct (no F1, no camera_manager Track)'
        return $true
    }
    $pythonIni = Join-Path $Docs 'cfg\python.ini'
    Write-AcSensorsParPresentationConfig -PythonIniPath $pythonIni `
        -PresentationMode:$PresentationMode -PresentationCamera $PresentationCamera `
        -PresentationChaseIndex $PresentationChaseIndex
    return $true
}

function Apply-AI-Presentation-Camera {
    param(
        [string]$Docs,
        [string]$RepoRoot,
        [switch]$DryRun
    )
    return (Apply-AI-Presentation-CameraConfig -Docs $Docs -RepoRoot $RepoRoot -DryRun:$DryRun)
}

function Set-AcCameraManagerOnboard {
    param([string]$TargetPath)
    $content = @(
        '[LAST_CAMERA]'
        'MODE=0'
        'DRIVABLE_MODE=0'
        ''
        '[FADE]'
        'COLOR=0,0,0'
        ''
        '[SHAKE]'
        'GFORCEX=1.4'
        'GFORCEZ=1.4'
        'GFORCEY=1.4'
        'SHAKEMULT=1.4'
    ) -join "`r`n"
    $parent = Split-Path -Parent $TargetPath
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Set-Content -LiteralPath $TargetPath -Value $content -Encoding UTF8
}

function Test-WorkerProfileValid {
    param(
        [int]$WorkerId,
        [string]$RepoRoot
    )
    $profileRoot = Get-WorkerProfileRoot -WorkerId $WorkerId -RepoRoot $RepoRoot
    if (-not (Test-Path -LiteralPath $profileRoot)) {
        return @{ Ok = $false; Error = ('profil absent: ' + $profileRoot) }
    }
    $raceIni = Join-Path $profileRoot 'cfg\race.ini'
    if (-not (Test-Path -LiteralPath $raceIni)) {
        return @{ Ok = $false; Error = 'race.ini absent' }
    }
    $cars = Get-IniValue -Path $raceIni -Section 'RACE' -Key 'CARS'
    $model = Get-IniValue -Path $raceIni -Section 'RACE' -Key 'MODEL'
    $track = Get-IniValue -Path $raceIni -Section 'RACE' -Key 'TRACK'
    if ($cars -ne '1') {
        return @{ Ok = $false; Error = ('CARS=' + [string]$cars + ' attendu 1') }
    }
    if (-not $model -or $model -notmatch '\S') {
        return @{ Ok = $false; Error = 'MODEL vide ou invalide' }
    }
    if (-not $track -or $track -notmatch '\S') {
        return @{ Ok = $false; Error = 'TRACK vide ou invalide' }
    }
    $vjoyName = Get-WorkerVjoyPresetName -WorkerId $WorkerId
    $vjoyPath = Join-Path $profileRoot ('cfg\controllers\savedsetups\' + $vjoyName)
    if (-not (Test-Path -LiteralPath $vjoyPath)) {
        return @{
            Ok    = $false
            Error = ($vjoyName + ' absent. Configurez vJoy device ' + ($WorkerId + 1) + ' puis Capture-AI-Worker-Settings.ps1 -WorkerId ' + $WorkerId)
        }
    }
    $workerYml = Join-Path $RepoRoot ('config\workers\worker_' + $WorkerId + '.yml')
    if (-not (Test-Path -LiteralPath $workerYml)) {
        return @{ Ok = $false; Error = ('config workers absent: ' + $workerYml) }
    }
    return @{ Ok = $true; ProfileRoot = $profileRoot }
}

function Invoke-SyncWorkerGearMapping {
    param(
        [Parameter(Mandatory)][int]$WorkerId,
        [Parameter(Mandatory)][string]$DocumentsRoot,
        [Parameter(Mandatory)][string]$RepoRoot
    )
    $venv = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
    $env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
    $py = @"
import json, sys
from AssettoCorsaEnv.gear_shift import prepare_worker_documents_controls
report = prepare_worker_documents_controls($WorkerId, r'''$DocumentsRoot''')
print(json.dumps(report))
if not report.get('valid'):
    sys.exit(1)
"@
    $result = Invoke-PythonCapturedProcess -PythonExe $venv -ArgumentList @('-c', $py) `
        -WorkingDirectory $RepoRoot -TimeoutSec 30
    if ($result.ExitCode -ne 0) {
        $err = $result.Stderr
        $line = ($result.Stdout -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
        if ($line) {
            try {
                $parsed = $line | ConvertFrom-Json
                if ($parsed.error) { $err = [string]$parsed.error }
            } catch { }
        }
        throw ('GEARUP sync worker ' + $WorkerId + ': ' + $err)
    }
}

function Invoke-DetectVjoyIndexMapping {
    <#
      Detect the REAL vJoy device_id -> joystick enumeration index mapping and
      persist it (outputs/diagnostics/vjoy_mapping_latest.json). Must run with all
      vJoy devices free (before any collector grabs one). This replaces the broken
      "JOY = worker_id" assumption: controls.ini will be patched with the detected
      index. GUARD RAIL: a non-zero exit means the mapping is incomplete/ambiguous,
      so we throw and refuse to launch (worker->car binding cannot be guaranteed).
    #>
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [string]$SessionStamp = '',
        [switch]$DryRun
    )
    if ($DryRun) {
        Write-Host '[DRY-RUN] detect vJoy device -> JOY index mapping (--save)'
        return
    }
    $venv = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
    $env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
    if ($SessionStamp) { $env:AC_AI_SESSION_ID = $SessionStamp }
    $result = Invoke-PythonCapturedProcess -PythonExe $venv `
        -ArgumentList @('-m', 'ac_workers.vjoy_index_probe', '--save') `
        -WorkingDirectory $RepoRoot -TimeoutSec 90
    if ($result.Stdout) { Write-Host ($result.Stdout.TrimEnd()) }
    if ($result.ExitCode -ne 0) {
        $err = $result.Stderr
        if (-not $err) { $err = $result.Stdout }
        throw ('vJoy index mapping detection failed (worker->car binding NOT safe): ' + $err)
    }
}

function Backup-AcDocumentsForMultiWorker {
    param(
        [string]$Docs,
        [string]$BackupRoot,
        [switch]$DryRun
    )
    $cfgFiles = Get-AcWorkerCfgFiles
    $cameraFiles = Get-AcCameraProfileFiles
    if ($DryRun) {
        Write-Host ('[DRY-RUN] backup Documents -> ' + $BackupRoot)
        return $BackupRoot
    }
    New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
    foreach ($rel in ($cfgFiles + $cameraFiles)) {
        $live = Join-Path $Docs $rel
        if (Test-Path -LiteralPath $live) {
            $bak = Join-Path $BackupRoot $rel
            $bakParent = Split-Path -Parent $bak
            if ($bakParent -and -not (Test-Path -LiteralPath $bakParent)) {
                New-Item -ItemType Directory -Path $bakParent -Force | Out-Null
            }
            Copy-Item -LiteralPath $live -Destination $bak -Force
        }
    }
    $savedLive = Join-Path $Docs 'cfg\controllers\savedsetups'
    if (Test-Path -LiteralPath $savedLive) {
        Copy-Item -LiteralPath $savedLive -Destination (Join-Path $BackupRoot 'cfg\controllers\savedsetups') -Recurse -Force
    }
    return $BackupRoot
}

function Apply-WorkerProfileToDocuments {
    param(
        [int]$WorkerId,
        [string]$Docs,
        [string]$RepoRoot,
        [switch]$DryRun,
        [switch]$OnboardCamera
    )
    $check = Test-WorkerProfileValid -WorkerId $WorkerId -RepoRoot $RepoRoot
    if (-not $check.Ok) { throw $check.Error }
    $profileRoot = $check.ProfileRoot
    $cfgFiles = Get-AcWorkerCfgFiles
    $cameraFiles = Get-AcCameraProfileFiles
    foreach ($rel in $cfgFiles) {
        $src = Join-Path $profileRoot $rel
        $dst = Join-Path $Docs $rel
        if (-not (Test-Path -LiteralPath $src)) { continue }
        if ($DryRun) {
            Write-Host ('[DRY-RUN] apply worker ' + $WorkerId + ': ' + $rel)
            continue
        }
        $dstParent = Split-Path -Parent $dst
        if ($dstParent -and -not (Test-Path -LiteralPath $dstParent)) {
            New-Item -ItemType Directory -Path $dstParent -Force | Out-Null
        }
        Copy-Item -LiteralPath $src -Destination $dst -Force
    }
    $vjoyName = Get-WorkerVjoyPresetName -WorkerId $WorkerId
    $vjoySrc = Join-Path $profileRoot ('cfg\controllers\savedsetups\' + $vjoyName)
    $setupsDst = Join-Path $Docs 'cfg\controllers\savedsetups'
    if ($DryRun) {
        Write-Host ('[DRY-RUN] apply vJoy preset ' + $vjoyName)
    } else {
        New-Item -ItemType Directory -Path $setupsDst -Force | Out-Null
        Copy-Item -LiteralPath $vjoySrc -Destination (Join-Path $setupsDst $vjoyName) -Force
        Write-SensorsParPythonIni -TargetPath (Join-Path $Docs 'cfg\python.ini') -WorkerId $WorkerId
        Invoke-SyncWorkerGearMapping -WorkerId $WorkerId -DocumentsRoot $Docs -RepoRoot $RepoRoot
        if ($OnboardCamera) {
            $camMgrLive = Join-Path $Docs 'cfg\camera_manager.ini'
            $camMgrProf = Join-Path $profileRoot 'cfg\camera_manager.ini'
            if (Test-Path -LiteralPath $camMgrProf) {
                Copy-Item -LiteralPath $camMgrProf -Destination $camMgrLive -Force
            } else {
                Set-AcCameraManagerOnboard -TargetPath $camMgrLive
            }
            foreach ($rel in $cameraFiles) {
                if ($rel -eq 'cfg\camera_manager.ini') { continue }
                $src = Join-Path $profileRoot $rel
                if (Test-Path -LiteralPath $src) {
                    Copy-Item -LiteralPath $src -Destination (Join-Path $Docs $rel) -Force
                }
            }
        } else {
            foreach ($rel in $cameraFiles) {
                $src = Join-Path $profileRoot $rel
                if (Test-Path -LiteralPath $src) {
                    Copy-Item -LiteralPath $src -Destination (Join-Path $Docs $rel) -Force
                }
            }
        }
    }
}

function Redirect-AcDocumentsToWorker {
    param(
        [Parameter(Mandatory)][int]$WorkerId,
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$Docs,
        [string]$LogPath = ''
    )
    $workerDocsRoot = Join-Path $RepoRoot ('ac_profiles\workers\worker_' + $WorkerId + '\Documents')
    $workerAc = Join-Path $workerDocsRoot 'Assetto Corsa'
    # Copier une fois le Documents complet vers le dossier du worker (39 Mo).
    if (-not (Test-Path -LiteralPath (Join-Path $workerAc 'cfg\controls.ini'))) {
        New-Item -ItemType Directory -Path $workerDocsRoot -Force | Out-Null
        Copy-Item -LiteralPath $Docs -Destination $workerDocsRoot -Recurse -Force
        if ($LogPath) { Write-AiSessionLog ('worker ' + $WorkerId + ' : Documents copie creee -> ' + $workerAc) $LogPath }
    }
    # Rediriger le dossier Documents (registre Personal) vers le dossier du worker.
    $userShell = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders'
    $shellFold = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders'
    $orig = [ordered]@{
        UserShell = (Get-ItemProperty -Path $userShell -Name Personal).Personal
        ShellFold = (Get-ItemProperty -Path $shellFold -Name Personal).Personal
    }
    Set-ItemProperty -Path $userShell -Name Personal -Value $workerDocsRoot -Type ExpandString
    Set-ItemProperty -Path $shellFold -Name Personal -Value $workerDocsRoot -Type String
    if ($LogPath) { Write-AiSessionLog ('worker ' + $WorkerId + ' : registre Personal redirige -> ' + $workerDocsRoot) $LogPath }
    return [ordered]@{ Orig = $orig; WorkerDocsRoot = $workerAc }
}

function Restore-AcDocumentsRedirect {
    param([Parameter(Mandatory)]$Redirect)
    if (-not $Redirect -or -not $Redirect.Orig) { return }
    $userShell = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders'
    $shellFold = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders'
    if ($Redirect.Orig.UserShell) {
        Set-ItemProperty -Path $userShell -Name Personal -Value $Redirect.Orig.UserShell -Type ExpandString
    }
    if ($Redirect.Orig.ShellFold) {
        Set-ItemProperty -Path $shellFold -Name Personal -Value $Redirect.Orig.ShellFold -Type String
    }
}

function Ensure-SteamClientReady {
    param(
        [string]$LogPath = '',
        [int]$TimeoutSec = 45
    )
    # A cold boot launches the normal Assetto Corsa menu unless Steam is already up
    # when acs.exe starts. Starting Steam first lets steam_appid.txt be honored.
    $steam = @(Get-Process -Name steam -ErrorAction SilentlyContinue)
    $helper = @(Get-Process -Name steamwebhelper -ErrorAction SilentlyContinue)
    if ($steam.Count -gt 0 -and $helper.Count -gt 0) { return }

    if ($steam.Count -eq 0) {
        $steamExe = $null
        foreach ($candidate in @(
            (Join-Path ${env:ProgramFiles(x86)} 'Steam\steam.exe'),
            (Join-Path $env:ProgramFiles 'Steam\steam.exe')
        )) {
            if ($candidate -and (Test-Path -LiteralPath $candidate)) { $steamExe = $candidate; break }
        }
        if (-not $steamExe) {
            try {
                $reg = Get-ItemProperty -Path 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue
                if ($reg.SteamExe -and (Test-Path -LiteralPath $reg.SteamExe)) { $steamExe = $reg.SteamExe }
            } catch { }
        }
        if ($steamExe) {
            # -offline : démarre Steam sans connexion Internet (le jeu n'a besoin
            # que du client Steam pour le DRM, pas du réseau). Évite le gros pic
            # de téléchargement à chaque lancement de worker.
            Start-Process -FilePath $steamExe -ArgumentList '-silent -offline' | Out-Null
            if ($LogPath) { Write-AiSessionLog 'Steam demarre en silence (mode hors ligne) avant les workers' $LogPath }
        } elseif ($LogPath) {
            Write-AiSessionLog 'WARN Steam introuvable: le menu officiel peut s ouvrir' $LogPath
        }
    }

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $steam = @(Get-Process -Name steam -ErrorAction SilentlyContinue)
        $helper = @(Get-Process -Name steamwebhelper -ErrorAction SilentlyContinue)
        if ($steam.Count -gt 0 -and $helper.Count -gt 0) {
            Start-Sleep -Seconds 2
            if ($LogPath) { Write-AiSessionLog 'Steam pret' $LogPath }
            return
        }
        Start-Sleep -Milliseconds 500
    }
    if ($LogPath) { Write-AiSessionLog 'WARN Steam pas pret avant le lancement des workers' $LogPath }
}

function Stop-StrayOfficialAcMenu {
    param(
        [string]$AiRoot,
        [string]$LogPath = ''
    )
    if (-not $AiRoot) { return }
    $ai = $AiRoot.TrimEnd('\').ToLowerInvariant()
    $procs = @(Get-Process -Name AssettoCorsa, acs -ErrorAction SilentlyContinue)
    foreach ($proc in $procs) {
        try {
            $path = [string]$proc.Path
            if (-not $path) { continue }
            $low = $path.ToLowerInvariant()
            if ($low.StartsWith($ai)) { continue }
            if ($low -notmatch '\\assettocorsa\\(assettocorsa|acs)\.exe$') { continue }
            Stop-Process -Id $proc.Id -Force -ErrorAction Stop
            if ($LogPath) {
                Write-AiSessionLog ('menu Assetto Corsa officiel ferme pid=' + $proc.Id) $LogPath
            }
        } catch { }
    }
}

function Start-AcOfficialMenuReaper {
    param(
        [string]$AiRoot,
        [string]$LogPath = '',
        [int]$Seconds = 15
    )
    # Steam can open the official Assetto Corsa menu (AssettoCorsa.exe) a few
    # seconds AFTER acs.exe connects to its DRM — outside the synchronous kill
    # window above. Reap it in the background so the AI game never gets a visible
    # "normal Corsa" window popping over it during worker boot.
    if (-not $AiRoot) { return }
    $ai = $AiRoot.TrimEnd('\').ToLowerInvariant()
    Start-Job -ScriptBlock {
        param($ai, $logPath, $seconds)
        $deadline = (Get-Date).AddSeconds($seconds)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 1500
            Get-Process -Name AssettoCorsa, acs -ErrorAction SilentlyContinue | ForEach-Object {
                try {
                    $p = [string]$_.Path
                    if (-not $p) { return }
                    $low = $p.ToLowerInvariant()
                    if ($low.StartsWith($ai)) { return }
                    if ($low -notmatch '\\assettocorsa\\(assettocorsa|acs)\.exe$') { return }
                    Stop-Process -Id $_.Id -Force -ErrorAction Stop
                    if ($logPath) {
                        $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
                        Add-Content -LiteralPath $logPath -Value ($ts + ' menu Assetto Corsa officiel ferme (reaper) pid=' + $_.Id)
                    }
                } catch { }
            }
        }
    } -ArgumentList $ai, $LogPath, $Seconds | Out-Null
}

function Ensure-AcSteamAppIdFile {
    param(
        [Parameter(Mandatory)][string]$AiRoot,
        [string]$LogPath = ''
    )
    # Steam relaunches AssettoCorsa.exe (official menu) unless acs.exe is tagged as app 244210.
    $path = Join-Path $AiRoot 'steam_appid.txt'
    $want = '244210'
    try {
        if (Test-Path -LiteralPath $path) {
            $cur = ((Get-Content -LiteralPath $path -Raw -ErrorAction SilentlyContinue) + '').Trim()
            if ($cur -eq $want) { return }
        }
        Set-Content -LiteralPath $path -Value $want -Encoding ASCII -NoNewline
        if ($LogPath) {
            Write-AiSessionLog 'steam_appid.txt set to 244210 (skip official launcher)' $LogPath
        }
    } catch {
        if ($LogPath) {
            Write-AiSessionLog ('WARN steam_appid.txt: ' + $_.Exception.Message) $LogPath
        }
    }
}

function Start-AcWorkerProcess {
    param(
        [int]$WorkerId,
        [string]$AiRoot,
        [string]$RepoRoot,
        [string]$LogPath,
        [switch]$PresentationMode,
        [string]$PresentationCamera = '',
        [int]$PresentationChaseIndex = 0,
        [switch]$DryRun
    )
    if ($DryRun) {
        Write-Host ('[DRY-RUN] acs.exe worker ' + $WorkerId + ' WD=' + $AiRoot)
        return $null
    }
    & (Join-Path $script:AcAiSessionCommonRoot 'tests\Test-Ac-Ini-Integrity.ps1') -RepoRoot $RepoRoot
    if ($LASTEXITCODE -ne 0) {
        throw ('INI integrity failed before worker ' + $WorkerId + ' launch')
    }
    $p = Get-WorkerPortMap -WorkerId $WorkerId
    if (-not $p) { throw ('WorkerId invalide: ' + $WorkerId) }
    Stop-StrayOfficialAcMenu -AiRoot $AiRoot -LogPath $LogPath
    Ensure-SteamClientReady -LogPath $LogPath
    Ensure-AcSteamAppIdFile -AiRoot $AiRoot -LogPath $LogPath
    Stop-StrayOfficialAcMenu -AiRoot $AiRoot -LogPath $LogPath
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = Join-Path $AiRoot 'acs.exe'
    $psi.WorkingDirectory = $AiRoot
    $psi.UseShellExecute = $false
    $joyIndex = Read-DetectedJoyIndex -VJoyDevice $p.VJoy -RepoRoot $RepoRoot
    $envBlock = @{
        ACGYM_WORKER_ID      = "$WorkerId"
        ACGYM_EGO_PORT       = "$($p.Ego)"
        ACGYM_OPPONENTS_PORT = "$($p.Opp)"
        ACGYM_MGMT_PORT      = "$($p.Mgmt)"
        ACGYM_VJOY_DEVICE    = "$($p.VJoy)"
        ACGYM_JOY_INDEX      = "$joyIndex"
        SteamAppId           = '244210'
        SteamGameId          = '244210'
    }
    if ($PresentationMode) {
        $envBlock['ACGYM_PRESENTATION_MODE'] = '1'
        if ($PresentationCamera) { $envBlock['ACGYM_PRESENTATION_CAMERA'] = $PresentationCamera }
        $envBlock['ACGYM_PRESENTATION_DRIVABLE_INDEX'] = "$PresentationChaseIndex"
    }
    foreach ($entry in [System.Environment]::GetEnvironmentVariables('Process').GetEnumerator()) {
        $k = [string]$entry.Key
        if (-not $psi.EnvironmentVariables.ContainsKey($k)) {
            [void]$psi.EnvironmentVariables.Add($k, [string]$entry.Value)
        }
    }
    foreach ($key in $envBlock.Keys) {
        if ($psi.EnvironmentVariables.ContainsKey($key)) {
            $psi.EnvironmentVariables[$key] = $envBlock[$key]
        } else {
            [void]$psi.EnvironmentVariables.Add($key, $envBlock[$key])
        }
    }
    $proc = [System.Diagnostics.Process]::Start($psi)
    if (-not $proc) { throw ('Echec demarrage acs.exe worker ' + $WorkerId) }
    Stop-StrayOfficialAcMenu -AiRoot $AiRoot -LogPath $LogPath
    Start-AcOfficialMenuReaper -AiRoot $AiRoot -LogPath $LogPath
    Write-AiSessionLog (
        'worker ' + $WorkerId + ' acs pid=' + $proc.Id `
        + ' ports ego=' + $p.Ego + ' mgmt=' + $p.Mgmt + ' vjoy=' + $p.VJoy
    ) $LogPath
    # Stamp the AC pid into this worker's controls snapshot meta: proves the AC
    # instance started AFTER the JOY patch that produced the snapshot it read.
    Invoke-RecordAcLaunchPid -WorkerId $WorkerId -AcPid $proc.Id -RepoRoot $RepoRoot -LogPath $LogPath
    return $proc
}

function Invoke-RecordAcLaunchPid {
    param(
        [Parameter(Mandatory)][int]$WorkerId,
        [Parameter(Mandatory)][int]$AcPid,
        [Parameter(Mandatory)][string]$RepoRoot,
        [string]$LogPath = ''
    )
    try {
        $venv = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $venv)) { return }
        $env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
        [void](Invoke-PythonCapturedProcess -PythonExe $venv -WorkingDirectory $RepoRoot -TimeoutSec 30 -ArgumentList @(
            '-m', 'ac_workers.controls_snapshot', '--record-ac-launch', '--worker', "$WorkerId", '--pid', "$AcPid"
        ))
    } catch {
        if ($LogPath) { Write-AiSessionLog ('WARN record ac pid worker ' + $WorkerId + ': ' + $_.Exception.Message) $LogPath }
    }
}

function Invoke-ClearControlsSnapshots {
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [switch]$DryRun
    )
    if ($DryRun) {
        Write-Host '[DRY-RUN] clear controls snapshots'
        return
    }
    try {
        $venv = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $venv)) { return }
        $env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
        [void](Invoke-PythonCapturedProcess -PythonExe $venv -WorkingDirectory $RepoRoot -TimeoutSec 30 -ArgumentList @(
            '-m', 'ac_workers.controls_snapshot', '--clear'
        ))
    } catch { }
}

function Invoke-WorkerHandshake {
    param(
        [int]$WorkerId,
        [string]$RepoRoot,
        [switch]$RequireTransition,
        [string]$SessionId = '',
        [int]$CollectorPid = 0,
        [switch]$DryRun
    )
    if ($DryRun) {
        Write-Host ('[DRY-RUN] handshake worker ' + $WorkerId)
        return $true
    }
    $venv = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
    $env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
    $args = @('-m', 'ac_workers.worker_handshake', '--worker-id', "$WorkerId")
    if ($RequireTransition) { $args += '--require-transition' }
    if ($SessionId) { $args += @('--session-id', $SessionId) }
    if ($CollectorPid -gt 0) { $args += @('--collector-pid', "$CollectorPid") }
    $hc = Invoke-PythonCapturedProcess -PythonExe $venv -ArgumentList $args -WorkingDirectory $RepoRoot -TimeoutSec 120
    return ($hc.ExitCode -eq 0)
}

function Test-TcpPortOpen {
    param([int]$Port, [string]$ComputerName = '127.0.0.1')
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect($ComputerName, $Port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(500, $false)
        if ($ok -and $client.Connected) {
            $client.Close()
            return $true
        }
        $client.Close()
    } catch {
    }
    return $false
}

function Test-AcsFromAiInstall {
    param(
        [string]$AiRoot,
        [int]$TimeoutSec = 600
    )
    $acsPath = (Join-Path $AiRoot 'acs.exe').ToLowerInvariant()
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        foreach ($proc in @(Get-Process -Name 'acs' -ErrorAction SilentlyContinue)) {
            try {
                $procPath = $proc.Path.ToLowerInvariant()
                if ($procPath -eq $acsPath) { return $proc }
            } catch {
            }
        }
        Start-Sleep -Seconds 2
    }
    return $null
}

function Get-WorkerManagementPort {
    param([Parameter(Mandatory)][int]$WorkerId)
    return (Get-WorkerPortMap -WorkerId $WorkerId).Mgmt
}

function Wait-ManagementPort {
    param(
        [int]$Port = 2347,
        [int]$TimeoutSec = 600,
        [int]$PollMs = 200
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-TcpPortOpen -Port $Port) { return $true }
        Start-Sleep -Milliseconds $PollMs
    }
    return $false
}

function Wait-AcWorkerLauncherReady {
    param(
        [Parameter(Mandatory)]$Process,
        [Parameter(Mandatory)][int]$MgmtPort,
        [Parameter(Mandatory)][int]$WorkerId,
        [Parameter(Mandatory)][string]$RepoRoot,
        [string]$LogPath = '',
        [int]$TimeoutSec = 120,
        [int]$PollMs = 200,
        [switch]$AllowPendingManagement
    )
    $timings = [ordered]@{
        acPidMs      = $null
        windowMs     = $null
        mgmtPortMs   = $null
        staticInfoMs = $null
        totalMs      = $null
    }
    $state = @{
        processAlive       = $false
        windowReady        = $false
        managementPortOpen = $false
        staticInfoValid    = $false
        elapsedMs          = 0
        lastError          = ''
        mgmtPort           = $MgmtPort
    }
    $prof = Test-WorkerProfileValid -WorkerId $WorkerId -RepoRoot $RepoRoot
    if (-not $prof.Ok) {
        $state.lastError = $prof.Error
        return @{ Ok = $false; TimingsMs = $timings; State = $state }
    }
    $raceIni = Join-Path $prof.ProfileRoot 'cfg\race.ini'
    $expectedCar = Get-IniValue -Path $raceIni -Section 'RACE' -Key 'MODEL'
    $expectedTrack = Get-IniValue -Path $raceIni -Section 'RACE' -Key 'TRACK'

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $stableHwnd = [IntPtr]::Zero
    $stableCount = 0

    while ((Get-Date) -lt $deadline) {
        $state.elapsedMs = $sw.ElapsedMilliseconds
        if (-not $Process -or $Process.HasExited) {
            $state.processAlive = $false
            $state.lastError = 'process exited'
            $timings.totalMs = $sw.ElapsedMilliseconds
            return @{ Ok = $false; TimingsMs = $timings; State = $state }
        }
        $state.processAlive = $true
        if ($null -eq $timings.acPidMs) {
            $timings.acPidMs = $sw.ElapsedMilliseconds
        }

        if (-not $state.windowReady) {
            $hwnd = [IntPtr]::Zero
            try {
                $Process.Refresh()
                $hwnd = $Process.MainWindowHandle
            } catch {
            }
            if (($hwnd -ne [IntPtr]::Zero)) {
                Initialize-AcSendInputNative
                if ([AcAiSendInput]::IsWindowVisible($hwnd)) {
                    if ($hwnd -eq $stableHwnd) {
                        $stableCount++
                    } else {
                        $stableHwnd = $hwnd
                        $stableCount = 1
                    }
                    if ($stableCount -ge 2) {
                        $state.windowReady = $true
                        $timings.windowMs = $sw.ElapsedMilliseconds
                    }
                }
            }
        }

        $state.managementPortOpen = (Test-TcpPortOpen -Port $MgmtPort)
        if ($state.managementPortOpen -and $null -eq $timings.mgmtPortMs) {
            $timings.mgmtPortMs = $sw.ElapsedMilliseconds
        }

        if ($state.managementPortOpen -and -not $state.staticInfoValid) {
            $info = Get-AcStaticInfo -Port $MgmtPort
            if (Test-AcStaticInfoCarTrack -StaticInfo $info -ExpectedCar $expectedCar -ExpectedTrack $expectedTrack) {
                $state.staticInfoValid = $true
                $timings.staticInfoMs = $sw.ElapsedMilliseconds
            } elseif ($info) {
                $state.lastError = (
                    'staticInfo car=' + [string]$info.CarName + ' track=' + [string]$info.TrackName
                )
            } else {
                $state.lastError = 'get_static_info empty on port ' + $MgmtPort
            }
        } elseif (-not $state.managementPortOpen) {
            $state.lastError = 'management port ' + $MgmtPort + ' closed'
        }

        if ($AllowPendingManagement -and $state.windowReady -and -not $state.managementPortOpen) {
            $timings.totalMs = $sw.ElapsedMilliseconds
            $state.elapsedMs = $timings.totalMs
            if ($LogPath) {
                if (-not $state.managementPortOpen) {
                    Write-AiSessionLog 'launcher ready: management port pending (collector will wait)' $LogPath
                } else {
                    Write-AiSessionLog (
                        'launcher ready worker=' + $WorkerId `
                        + ' profile+launch=' + $timings.acPidMs + 'ms window=' + $timings.windowMs `
                        + 'ms port=' + $timings.mgmtPortMs + 'ms static=' + $timings.staticInfoMs `
                        + 'ms total=' + $timings.totalMs + 'ms'
                    ) $LogPath
                }
            }
            return @{ Ok = $true; TimingsMs = $timings; State = $state }
        }

        if ($state.windowReady -and $state.managementPortOpen -and $state.staticInfoValid) {
            $timings.totalMs = $sw.ElapsedMilliseconds
            $state.elapsedMs = $timings.totalMs
            if ($LogPath) {
                Write-AiSessionLog (
                    'launcher ready worker=' + $WorkerId `
                    + ' profile+launch=' + $timings.acPidMs + 'ms window=' + $timings.windowMs `
                    + 'ms port=' + $timings.mgmtPortMs + 'ms static=' + $timings.staticInfoMs `
                    + 'ms total=' + $timings.totalMs + 'ms'
                ) $LogPath
            }
            return @{ Ok = $true; TimingsMs = $timings; State = $state }
        }
        Start-Sleep -Milliseconds $PollMs
    }
    $timings.totalMs = $sw.ElapsedMilliseconds
    $state.elapsedMs = $timings.totalMs
    if (-not $state.lastError) { $state.lastError = 'timeout' }
    if ($LogPath) {
        Write-AiSessionLog (
            'launcher timeout worker=' + $WorkerId + ' state=' + ($state | ConvertTo-Json -Compress)
        ) $LogPath
    }
    return @{ Ok = $false; TimingsMs = $timings; State = $state }
}

function Write-AcWorkerLaunchTimings {
    param(
        [int]$WorkerId,
        [hashtable]$Timings,
        [string]$LogPath,
        [string]$OrchestratorLog = ''
    )
    $line = (
        'timings worker ' + $WorkerId `
        + ' profilePrepMs=' + $Timings.profilePrepMs `
        + ' cmGoMs=' + [string]$Timings.cmGoMs `
        + ' acsStartMs=' + [string]$Timings.acsStartMs `
        + ' windowMs=' + [string]$Timings.windowMs `
        + ' driveMs=' + [string]$Timings.driveMs `
        + ' mgmtPortMs=' + [string]$Timings.mgmtPortMs `
        + ' staticInfoMs=' + [string]$Timings.staticInfoMs `
        + ' totalMs=' + $Timings.totalMs
    )
    if ($LogPath) { Write-AiSessionLog $line $LogPath }
    if ($OrchestratorLog) { Write-AiSessionLog $line $OrchestratorLog }
}

function Get-BootstrapLogTail {
    param(
        [string]$AiRoot,
        [int]$Lines = 30
    )
    $logPath = Join-Path $AiRoot 'apps\python\sensors_par\sensors_par_bootstrap.log'
    if (-not (Test-Path -LiteralPath $logPath)) { return '(bootstrap log absent: ' + $logPath + ')' }
    return (Get-Content -LiteralPath $logPath -Tail $Lines -ErrorAction SilentlyContinue) -join "`n"
}

function Test-IncompleteV3Output {
    param([string]$WorkDir)
    if (-not (Test-Path -LiteralPath $WorkDir)) { return $false }
    $final = Join-Path $WorkDir 'model\final'
    if (Test-Path -LiteralPath $final) { return $false }
    $ckpt = Join-Path $WorkDir 'model\checkpoints'
    if (Test-Path -LiteralPath $ckpt) {
        $files = @(Get-ChildItem -LiteralPath $ckpt -File -ErrorAction SilentlyContinue)
        if ((Get-CollectionCount $files) -gt 0) { return $true }
    }
    $log = Join-Path $WorkDir 'log.log'
    if (Test-Path -LiteralPath $log) { return $true }
    return $false
}

function Initialize-AcSendInputNative {
    if ($script:AcSendInputLoaded) { return }
    Add-Type @'
using System;
using System.Runtime.InteropServices;

public class AcAiSendInput {
    [StructLayout(LayoutKind.Sequential)]
    public struct INPUT {
        public uint type;
        public InputUnion U;
    }
    [StructLayout(LayoutKind.Explicit)]
    public struct InputUnion {
        [FieldOffset(0)] public KEYBDINPUT ki;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct KEYBDINPUT {
        public ushort wVk;
        public ushort wScan;
        public uint dwFlags;
        public uint time;
        public IntPtr dwExtraInfo;
    }
    public const uint INPUT_KEYBOARD = 1;
    public const uint KEYEVENTF_KEYUP = 0x0002;
    public const ushort VK_RETURN = 0x0D;
    public const ushort VK_ESCAPE = 0x1B;
    public const ushort VK_LMENU = 0xA4;
    public const ushort VK_RMENU = 0xA5;
    public const ushort VK_LCONTROL = 0xA2;
    public const ushort VK_RCONTROL = 0xA3;
    public const ushort VK_LSHIFT = 0xA0;
    public const ushort VK_RSHIFT = 0xA1;
    public const ushort VK_LWIN = 0x5B;
    public const ushort VK_RWIN = 0x5C;
    public const int SW_RESTORE = 9;

    [DllImport("user32.dll", SetLastError=true)]
    public static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    public static void SendKey(ushort vk, bool keyUp) {
        INPUT[] inputs = new INPUT[1];
        inputs[0].type = INPUT_KEYBOARD;
        inputs[0].U.ki.wVk = vk;
        inputs[0].U.ki.dwFlags = keyUp ? KEYEVENTF_KEYUP : 0u;
        SendInput(1, inputs, Marshal.SizeOf(typeof(INPUT)));
    }

    public static void ReleaseModifier(ushort vk) {
        SendKey(vk, true);
    }

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("kernel32.dll")]
    public static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern bool SetFocus(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern short GetAsyncKeyState(int vKey);

    public static bool IsKeyDown(int vk) {
        return (GetAsyncKeyState(vk) & 0x8000) != 0;
    }

    public static void FocusWindowNoKeyboard(IntPtr hWnd) {
        if (hWnd == IntPtr.Zero) { return; }
        ShowWindow(hWnd, SW_RESTORE);
        IntPtr fg = GetForegroundWindow();
        uint fgThread = 0;
        uint targetThread = 0;
        if (fg != IntPtr.Zero) {
            GetWindowThreadProcessId(fg, out fgThread);
        }
        GetWindowThreadProcessId(hWnd, out targetThread);
        uint curThread = GetCurrentThreadId();
        bool attachedFg = false;
        bool attachedTarget = false;
        if (fgThread != 0 && fgThread != curThread) {
            attachedFg = AttachThreadInput(curThread, fgThread, true);
        }
        if (targetThread != 0 && targetThread != curThread) {
            attachedTarget = AttachThreadInput(curThread, targetThread, true);
        }
        SetForegroundWindow(hWnd);
        SetFocus(hWnd);
        if (attachedTarget) { AttachThreadInput(curThread, targetThread, false); }
        if (attachedFg) { AttachThreadInput(curThread, fgThread, false); }
    }

    public static bool ActivateForeground(IntPtr hWnd) {
        if (hWnd == IntPtr.Zero) { return false; }
        ShowWindow(hWnd, SW_RESTORE);
        FocusWindowNoKeyboard(hWnd);
        return GetForegroundWindow() == hWnd;
    }

    public const uint WM_MOUSEMOVE = 0x0200;
    public const uint WM_LBUTTONDOWN = 0x0201;
    public const uint WM_LBUTTONUP = 0x0202;
    public const uint WM_KEYDOWN = 0x0100;
    public const uint WM_KEYUP = 0x0101;

    [DllImport("user32.dll", SetLastError=true)]
    public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
        public int Width { get { return Right - Left; } }
        public int Height { get { return Bottom - Top; } }
    }

    [DllImport("user32.dll")]
    public static extern bool GetClientRect(IntPtr hWnd, out RECT lpRect);

    public static IntPtr MakeLParam(int x, int y) {
        return (IntPtr)((y << 16) | (x & 0xFFFF));
    }

    public static void SendClientClick(IntPtr hWnd, int x, int y) {
        IntPtr lp = MakeLParam(x, y);
        PostMessage(hWnd, WM_MOUSEMOVE, IntPtr.Zero, lp);
        PostMessage(hWnd, WM_LBUTTONDOWN, (IntPtr)1, lp);
        PostMessage(hWnd, WM_LBUTTONUP, IntPtr.Zero, lp);
    }

    public static void SendEnterKeyToWindow(IntPtr hWnd) {
        PostMessage(hWnd, WM_KEYDOWN, (IntPtr)VK_RETURN, IntPtr.Zero);
        PostMessage(hWnd, WM_KEYUP, (IntPtr)VK_RETURN, (IntPtr)0xC0000001);
    }

    public static void SendEscapeKeyToWindow(IntPtr hWnd) {
        PostMessage(hWnd, WM_KEYDOWN, (IntPtr)VK_ESCAPE, IntPtr.Zero);
        PostMessage(hWnd, WM_KEYUP, (IntPtr)VK_ESCAPE, (IntPtr)0xC0000001);
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct POINT { public int X; public int Y; }

    [DllImport("user32.dll")]
    public static extern bool ClientToScreen(IntPtr hWnd, ref POINT lpPoint);

    [DllImport("user32.dll")]
    public static extern IntPtr GetDC(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern int ReleaseDC(IntPtr hWnd, IntPtr hDC);

    [DllImport("gdi32.dll")]
    public static extern uint GetPixel(IntPtr hdc, int nXPos, int nYPos);

    public const uint CLR_INVALID = 0xFFFFFFFF;

    // Lit le pixel ecran correspondant a un point client (la fenetre doit etre visible
    // au premier plan a cet endroit). Retourne 0x00BBGGRR, ou -1 si echec.
    public static int GetScreenPixel(IntPtr hWnd, int clientX, int clientY) {
        POINT p; p.X = clientX; p.Y = clientY;
        if (!ClientToScreen(hWnd, ref p)) { return -1; }
        IntPtr hdc = GetDC(IntPtr.Zero);
        if (hdc == IntPtr.Zero) { return -1; }
        uint c = GetPixel(hdc, p.X, p.Y);
        ReleaseDC(IntPtr.Zero, hdc);
        if (c == CLR_INVALID) { return -1; }
        return (int)(c & 0x00FFFFFF);
    }

    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int X, int Y);

    [DllImport("user32.dll")]
    public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, int dwExtraInfo);

    public const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    public const uint MOUSEEVENTF_LEFTUP = 0x0004;

    // AC ignore PostMessage: il lit GetCursorPos. Vrai clic ecran sur le volant.
    public static bool SendRealClientClick(IntPtr hWnd, int x, int y) {
        POINT p; p.X = x; p.Y = y;
        if (!ClientToScreen(hWnd, ref p)) { return false; }
        SetCursorPos(p.X, p.Y);
        mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0);
        mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0);
        return true;
    }
}
'@
    $script:AcSendInputLoaded = $true
}

function Release-StuckModifierKeys {
    param([string]$LogPath)
    Initialize-AcSendInputNative
    $keys = @(
        @{ Name = 'LAlt'; Vk = [AcAiSendInput]::VK_LMENU },
        @{ Name = 'RAlt'; Vk = [AcAiSendInput]::VK_RMENU },
        @{ Name = 'LCtrl'; Vk = [AcAiSendInput]::VK_LCONTROL },
        @{ Name = 'RCtrl'; Vk = [AcAiSendInput]::VK_RCONTROL },
        @{ Name = 'LShift'; Vk = [AcAiSendInput]::VK_LSHIFT },
        @{ Name = 'RShift'; Vk = [AcAiSendInput]::VK_RSHIFT },
        @{ Name = 'LWin'; Vk = [AcAiSendInput]::VK_LWIN },
        @{ Name = 'RWin'; Vk = [AcAiSendInput]::VK_RWIN }
    )
    for ($round = 1; $round -le 5; $round++) {
        foreach ($k in $keys) {
            [AcAiSendInput]::ReleaseModifier($k.Vk) | Out-Null
        }
        Start-Sleep -Milliseconds 30
        $down = @()
        foreach ($k in $keys) {
            if ([AcAiSendInput]::IsKeyDown([int]$k.Vk)) { $down += $k.Name }
        }
        if ((Get-CollectionCount $down) -eq 0) {
            $msg = 'modifiers released OK round=' + $round
            if ($LogPath) { Write-AiSessionLog $msg $LogPath }
            return
        }
    }
    $final = @()
    foreach ($k in $keys) {
        if ([AcAiSendInput]::IsKeyDown([int]$k.Vk)) { $final += $k.Name }
    }
    $warn = 'modifiers still down: ' + ($final -join ', ')
    if ($LogPath) { Write-AiSessionLog $warn $LogPath }
    Write-Host $warn
}

function Focus-WindowWithoutAlt {
    param([IntPtr]$WindowHandle)
    if ($WindowHandle -eq [IntPtr]::Zero) { return $false }
    Initialize-AcSendInputNative
    [AcAiSendInput]::FocusWindowNoKeyboard($WindowHandle) | Out-Null
    return $true
}

function Hide-SessionConsoleInPresentationMode {
    try {
        if (-not ([System.Management.Automation.PSTypeName]'ConsoleWin').Type) {
            Add-Type -Namespace ConsoleWin -Name Native -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern System.IntPtr GetConsoleWindow();
[DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
'@
        }
        $hwnd = [ConsoleWin.Native]::GetConsoleWindow()
        if ($hwnd -ne [IntPtr]::Zero) {
            [ConsoleWin.Native]::ShowWindow($hwnd, 6) | Out-Null
        }
    } catch {
    }
}

function Send-AcEnterKeyPress {
    Initialize-AcSendInputNative
    [AcAiSendInput]::SendKey([AcAiSendInput]::VK_RETURN, $false) | Out-Null
    Start-Sleep -Milliseconds 80
    [AcAiSendInput]::SendKey([AcAiSendInput]::VK_RETURN, $true) | Out-Null
}

function Get-AcDrivingState {
    param([int]$Port = 2347)
    $raw = Invoke-AcManagementCommand -Command 'get_driving_state' -Port $Port
    if (-not $raw) { return $null }
    try {
        return ($raw | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Test-AcDriveEngaged {
    param($DrivingState)
    if (-not $DrivingState) { return $false }
    # En mode HOTLAP, l'horloge de session AVANCE deja dans le menu Drive -> se fier a
    # session_clock_advancing donnait un faux "Drive confirmed" alors que la voiture
    # n'avait jamais quitte le menu. Signal fiable: dans le menu, AC bloque la voiture
    # frein a fond (menu_brake_hold). Tant que le frein est tenu, on N'EST PAS en Drive.
    if ($DrivingState.menu_brake_hold -eq $true) { return $false }
    if (-not $DrivingState.driving) { return $false }
    # Le plugin a deja exclu le menu (frein relache + sim live) dans 'driving'.
    return $true
}

function Test-AcDriveSessionLive {
    param($DrivingState)
    if (-not $DrivingState) { return $false }
    return [bool]$DrivingState.driving
}

function Test-AcDriveLauncherMgmtFallbackOk {
    param(
        [int]$MgmtPort,
        [string]$LogPath = ''
    )
    if (-not (Test-TcpPortOpen -Port $MgmtPort)) { return $false }
    $state = Get-AcDrivingState -Port $MgmtPort
    if ($state) { return $false }
    $static = Get-AcStaticInfo -Port $MgmtPort
    if (-not (Test-AcStaticInfoCarTrack -StaticInfo $static)) { return $false }
    if ($LogPath) {
        Write-AiSessionLog 'Drive fallback: mgmt static info OK but get_driving_state unavailable (redeploy plugin if this persists)' $LogPath
    }
    return $true
}

function Write-AcDrivingStateDiagnostic {
    param(
        $DrivingState,
        [string]$LogPath = ''
    )
    if (-not $DrivingState) {
        if ($LogPath) { Write-AiSessionLog 'Drive state unavailable from management port' $LogPath }
        return
    }
    $msg = (
        'Drive state driving=' + $DrivingState.driving `
        + ' controls_live=' + $DrivingState.controls_live `
        + ' brake=' + $DrivingState.physics_brake `
        + ' menu_brake_hold=' + $DrivingState.menu_brake_hold `
        + ' clock_adv=' + $DrivingState.session_clock_advancing `
        + ' ac_status=' + $DrivingState.ac_status `
        + ' reason=' + $DrivingState.reason
    )
    if ($LogPath) { Write-AiSessionLog $msg $LogPath }
}

function Get-AcDriveWheelClickPoints {
    # Volant Drive: 38,78 etait 3 cm trop haut. +114 px (~3 cm a 96 DPI).
    # JAMAIS $arr += @{...} : PowerShell fusionne les hashtables.
    param([Parameter(Mandatory)][IntPtr]$WindowHandle)
    Initialize-AcSendInputNative
    $list = New-Object 'System.Collections.Generic.List[object]'
    $clientW = 832
    $clientH = 464
    $rect = New-Object AcAiSendInput+RECT
    if ([AcAiSendInput]::GetClientRect($WindowHandle, [ref]$rect)) {
        $clientW = [Math]::Max(1, $rect.Width)
        $clientH = [Math]::Max(1, $rect.Height)
    }
    if ($clientW -lt 400 -or $clientH -lt 250) {
        return $list
    }
    foreach ($pair in @(
        @(38, 192), @(48, 192), @(38, 200), @(48, 200)
    )) {
        $x = [int]$pair[0]
        $y = [int]$pair[1]
        if ($x -lt $clientW -and $y -lt $clientH) {
            $list.Add((New-Object psobject -Property @{ X = $x; Y = $y }))
        }
    }
    return $list
}

function Get-AcClickInt {
    param($Value)
    if ($null -eq $Value) { return 0 }
    if ($Value -is [System.Array]) { return [int](@($Value)[0]) }
    return [int]$Value
}

function Get-AcDriveClickClientPoint {
    param([IntPtr]$WindowHandle)
    $pts = @(Get-AcDriveWheelClickPoints -WindowHandle $WindowHandle)
    if ((Get-CollectionCount $pts) -eq 0) { return $null }
    return $pts[0]
}

function Get-AcDriveClickCandidates {
    param([IntPtr]$WindowHandle)
    return @(Get-AcDriveWheelClickPoints -WindowHandle $WindowHandle)
}

function Get-AcDriveButtonClickPoints {
    param([Parameter(Mandatory)][IntPtr]$WindowHandle)
    return @(Get-AcDriveWheelClickPoints -WindowHandle $WindowHandle)
}

function Invoke-AcDriveFixedClick {
    # Tous les workers: cliquer le volant (PostMessage dans CETTE fenetre).
    # Ne pas croire get_driving_state avant le clic: en multi-instance la physique
    # lue est souvent celle du worker 0 deja en piste.
    param(
        [Parameter(Mandatory)][IntPtr]$WindowHandle,
        [Parameter(Mandatory)]$Process,
        [int]$MgmtPort = 2347,
        [int]$TimeoutMs = 10000,
        [int]$PollMs = 400,
        [string]$LogPath = ''
    )
    Initialize-AcSendInputNative
    $pts = @(Get-AcDriveWheelClickPoints -WindowHandle $WindowHandle)
    if ($LogPath) {
        $n = Get-CollectionCount $pts
        if ($n -gt 0) {
            $sample = $pts[0]
            Write-AiSessionLog ('Drive wheel clicks n=' + $n + ' first=' + $sample.X + ',' + $sample.Y) $LogPath
        } else {
            Write-AiSessionLog 'Drive wheel clicks n=0 (client trop petit)' $LogPath
        }
    }
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMs)
    $clicks = 0
    $rounds = 0
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Process.HasExited) {
            if ($LogPath) { Write-AiSessionLog 'Drive click: AC process exited' $LogPath }
            return $false
        }
        if ((Get-CollectionCount $pts) -eq 0) { break }
        [AcAiSendInput]::ActivateForeground($WindowHandle) | Out-Null
        foreach ($pt in $pts) {
            $cx = Get-AcClickInt $pt.X
            $cy = Get-AcClickInt $pt.Y
            if ($cx -le 0 -or $cy -le 0) { continue }
            # PostMessage seul est ignore par AC (lit GetCursorPos).
            [AcAiSendInput]::SendClientClick($WindowHandle, $cx, $cy)
            Start-Sleep -Milliseconds 80
            [void][AcAiSendInput]::SendRealClientClick($WindowHandle, $cx, $cy)
            $clicks++
        }
        $rounds++
        if ($LogPath) {
            Write-AiSessionLog ('Drive wheel click round=' + $rounds + ' total=' + $clicks) $LogPath
        }
        Start-Sleep -Milliseconds $PollMs
        if ($rounds -ge 2) { break }
    }
    if ($LogPath) {
        Write-AiSessionLog ('Drive wheel click done rounds=' + $rounds + ' clicks=' + $clicks) $LogPath
    }
    return ($clicks -gt 0 -and (Test-AcProcessWindowAlive -Process $Process))
}

function Invoke-AcDriveClickScan {
    # Clique chaque candidat de la barre laterale, verifie l'etat Drive entre chaque.
    param(
        [Parameter(Mandatory)][IntPtr]$WindowHandle,
        [Parameter(Mandatory)]$Process,
        [int]$MgmtPort = 2347,
        [int]$PerClickPollMs = 700,
        [string]$LogPath = ''
    )
    $cands = @(Get-AcDriveClickCandidates -WindowHandle $WindowHandle)
    if ($LogPath) {
        Write-AiSessionLog ('Drive click scan: ' + (Get-CollectionCount $cands) + ' candidats barre laterale') $LogPath
    }
    $i = 0
    foreach ($pt in $cands) {
        $i++
        if (-not (Test-AcProcessWindowAlive -Process $Process)) {
            if ($LogPath) { Write-AiSessionLog 'Drive click scan aborted: AC process exited' $LogPath }
            return $false
        }
        if (Test-AcDriveEngagedNow -MgmtPort $MgmtPort) {
            if ($LogPath) { Write-AiSessionLog 'Drive click scan: already engaged, stop' $LogPath }
            return $true
        }
        Initialize-AcSendInputNative
        [AcAiSendInput]::ActivateForeground($WindowHandle) | Out-Null
        Release-StuckModifierKeys -LogPath $LogPath
        $cx = Get-AcClickInt $pt.X
        $cy = Get-AcClickInt $pt.Y
        [AcAiSendInput]::SendClientClick($WindowHandle, $cx, $cy)
        if ($LogPath) {
            Write-AiSessionLog ('Drive scan click ' + $i + '/' + (Get-CollectionCount $cands) + ' client=' + $cx + ',' + $cy) $LogPath
        }
        if (Wait-AcDriveEngagedPoll -MgmtPort $MgmtPort -TimeoutMs $PerClickPollMs -PollMs 150 -RequireControlsLive) {
            if ($LogPath) {
                Write-AiSessionLog ('Drive confirmed via scan click ' + $i + ' at ' + $cx + ',' + $cy) $LogPath
            }
            return $true
        }
    }
    return $false
}

function Test-AcDriveEngagedNow {
    param([int]$MgmtPort)
    $state = Get-AcDrivingState -Port $MgmtPort
    return (Test-AcDriveEngaged -DrivingState $state)
}

function Test-AcProcessWindowAlive {
    param(
        [Parameter(Mandatory)]$Process
    )
    try {
        if (-not $Process -or $Process.HasExited) { return $false }
        $Process.Refresh()
        $hwnd = $Process.MainWindowHandle
        if ($hwnd -eq [IntPtr]::Zero) { return $false }
        Initialize-AcSendInputNative
        return [AcAiSendInput]::IsWindowVisible($hwnd)
    } catch {
        return $false
    }
}

function Wait-AcDriveEngagedPoll {
    param(
        [int]$MgmtPort,
        [int]$TimeoutMs = 2000,
        [int]$PollMs = 200,
        [switch]$RequireControlsLive,
        [int]$StableReads = 1,
        [string]$LogPath = ''
    )
    # Anti-flicker: require StableReads CONSECUTIVE engaged reads before declaring
    # Drive. A single transient "brake released" frame (e.g. right after a
    # resume_drive ENTER nudge) is NOT enough to confirm we left the menu. The
    # collector still applies the authoritative truth gate afterwards.
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMs)
    $lastState = $null
    $consecutive = 0
    while ([DateTime]::UtcNow -lt $deadline) {
        $state = Get-AcDrivingState -Port $MgmtPort
        $lastState = $state
        $ok = if ($RequireControlsLive) {
            (Test-AcDriveEngaged -DrivingState $state)
        } else {
            (Test-AcDriveSessionLive -DrivingState $state)
        }
        if ($ok) {
            $consecutive++
            if ($consecutive -ge [Math]::Max(1, $StableReads)) {
                return $true
            }
        } else {
            $consecutive = 0
        }
        Start-Sleep -Milliseconds $PollMs
    }
    if ($LogPath) {
        Write-AcDrivingStateDiagnostic -DrivingState $lastState -LogPath $LogPath
    }
    return $false
}

function Wait-AcClientUiStable {
    param(
        [Parameter(Mandatory)][IntPtr]$WindowHandle,
        [string]$LogPath = '',
        [int]$StableReads = 3,
        [int]$GapMs = 150
    )
    Initialize-AcSendInputNative
    $lastW = -1
    $lastH = -1
    $stable = 0
    while ($stable -lt $StableReads) {
        $rect = New-Object AcAiSendInput+RECT
        if (-not [AcAiSendInput]::GetClientRect($WindowHandle, [ref]$rect)) {
            return $false
        }
        $w = [int]$rect.Width
        $h = [int]$rect.Height
        if ($w -eq $lastW -and $h -eq $lastH) {
            $stable++
        } else {
            $stable = 1
            $lastW = $w
            $lastH = $h
        }
        Start-Sleep -Milliseconds $GapMs
    }
    if ($LogPath) { Write-AiSessionLog 'Drive UI stable' $LogPath }
    return $true
}

function Send-AcDriveClickAttempt {
    param(
        [Parameter(Mandatory)][IntPtr]$WindowHandle,
        [int]$Attempt,
        [int]$MaxAttempts,
        [hashtable]$Point,
        [string]$LogPath = ''
    )
    Initialize-AcSendInputNative
    $activated = [AcAiSendInput]::ActivateForeground($WindowHandle)
    if (-not $activated) {
        if ($LogPath) {
            Write-AiSessionLog 'Drive window activation failed (PostMessage fallback)' $LogPath
        }
    } elseif ($Attempt -eq 1 -and $LogPath) {
        Write-AiSessionLog 'Drive window activated' $LogPath
    }
    Release-StuckModifierKeys -LogPath $LogPath
    $cx = Get-AcClickInt $Point.X
    $cy = Get-AcClickInt $Point.Y
    [AcAiSendInput]::SendClientClick($WindowHandle, $cx, $cy)
    if ($LogPath) {
        Write-AiSessionLog (
            'Drive click attempt ' + $Attempt + '/' + $MaxAttempts `
            + ' client=' + $cx + ',' + $cy
        ) $LogPath
    }
    return $true
}

function Send-AcResumeDrive {
    param(
        [IntPtr]$WindowHandle = [IntPtr]::Zero,
        [int]$MgmtPort = 2347,
        [string]$LogPath = ''
    )
    Invoke-AcManagementCommand -Command 'resume_drive' -Port $MgmtPort -TimeoutMs 600 | Out-Null
    if ($LogPath) { Write-AiSessionLog 'resume_drive sent via mgmt port (plugin wheel click)' $LogPath }
}

function Test-AcDriveClientReady {
    param([Parameter(Mandatory)][IntPtr]$WindowHandle)
    Initialize-AcSendInputNative
    $rect = New-Object AcAiSendInput+RECT
    if (-not [AcAiSendInput]::GetClientRect($WindowHandle, [ref]$rect)) { return $false }
    return ([int]$rect.Width -ge 400 -and [int]$rect.Height -ge 250)
}

function Test-AcDriveClickEnabled {
    param([string]$RepoRoot)
    if (-not $RepoRoot) { return $true }
    $pref = Join-Path $RepoRoot 'ac_workers\status\drive_click.json'
    if (-not (Test-Path -LiteralPath $pref)) { return $true }
    try {
        $j = Get-Content -LiteralPath $pref -Raw | ConvertFrom-Json
        if ($null -eq $j.enabled) { return $true }
        return [bool]$j.enabled
    } catch {
        return $true
    }
}

function Invoke-AcDriveActivation {
    param(
        [Parameter(Mandatory)]$Process,
        [string]$LogPath,
        [int]$MaxClickAttempts = 3,
        [int]$MgmtPort = 2347,
        [switch]$PresentationMode,
        [string]$RepoRoot
    )
    if (-not $RepoRoot) {
        $RepoRoot = (Resolve-Path (Join-Path $script:AcAiSessionCommonRoot '..')).Path
    }
    Write-AiSessionLog 'Drive: attente fenetre AC (max 120s)' $LogPath
    $hwnd = Wait-AcMainWindowReady -Process $Process -TimeoutSec 120 -LogPath $LogPath
    if ($hwnd -eq [IntPtr]::Zero) {
        Write-AiSessionLog 'fenetre AC non prete' $LogPath
        Write-AiSessionLog 'Drive input not delivered: AC window not ready' $LogPath
        return $false
    }
    Write-AiSessionLog 'fenetre AC prete' $LogPath
    if (-not (Wait-AcClientUiStable -WindowHandle $hwnd -LogPath $LogPath)) {
        Write-AiSessionLog 'Drive UI stable check failed' $LogPath
    }
    $sizeDeadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $sizeDeadline) {
        if ($Process.HasExited) {
            Write-AiSessionLog 'Drive input not delivered: AC process exited' $LogPath
            return $false
        }
        $Process.Refresh()
        if ($Process.MainWindowHandle -ne [IntPtr]::Zero) { $hwnd = $Process.MainWindowHandle }
        if (Test-AcDriveClientReady -WindowHandle $hwnd) { break }
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-AcDriveClientReady -WindowHandle $hwnd)) {
        Write-AiSessionLog 'Drive input not delivered: AC client too small (still loading)' $LogPath
        return $false
    }
    Write-AiSessionLog 'Drive: pause 3s pour le menu session (pas de clic pendant le chargement)' $LogPath
    Start-Sleep -Seconds 3
    Write-AiSessionLog ('Drive: attente plugin mgmt port=' + $MgmtPort) $LogPath
    if (-not (Wait-ManagementPort -Port $MgmtPort -TimeoutSec 90)) {
        Write-AiSessionLog 'Drive: management port not open after 90s (plugin click may fail)' $LogPath
    }
    $menuDeadline = (Get-Date).AddSeconds(45)
    $sawMenu = $false
    while ((Get-Date) -lt $menuDeadline) {
        if ($Process.HasExited) {
            Write-AiSessionLog 'Drive input not delivered: AC process exited' $LogPath
            return $false
        }
        $st = Get-AcDrivingState -Port $MgmtPort
        Stop-StrayOfficialAcMenu -AiRoot (Join-Path $RepoRoot 'AssettoCorsa-AI') -LogPath $LogPath
        if ($st -and $st.menu_brake_hold -eq $true) {
            $sawMenu = $true
            break
        }
        Start-Sleep -Milliseconds 250
    }
    Write-AiSessionLog ('Drive menu_brake_hold=' + $sawMenu) $LogPath
    $aiRoot = Join-Path $RepoRoot 'AssettoCorsa-AI'
    Stop-StrayOfficialAcMenu -AiRoot $aiRoot -LogPath $LogPath
    if (-not (Test-AcDriveClickEnabled -RepoRoot $RepoRoot)) {
        Write-AiSessionLog 'Drive auto-click OFF — attente clic manuel (90s)' $LogPath
        if (Wait-AcDriveEngagedPoll -MgmtPort $MgmtPort -TimeoutMs 90000 -PollMs 400 -RequireControlsLive -LogPath $LogPath) {
            Write-AiSessionLog 'Drive confirmed via manual click' $LogPath
            if ($PresentationMode) {
                Write-AiSessionLog 'presentation camera handled by plugin acUpdate' $LogPath
            }
            return $true
        }
        Write-AiSessionLog 'Drive input not delivered: manual click timeout' $LogPath
        return $false
    }
    $engageDeadline = (Get-Date).AddSeconds(25)
    $round = 0
    while ((Get-Date) -lt $engageDeadline) {
        $round++
        if ($Process.HasExited) {
            Write-AiSessionLog 'Drive input not delivered: AC process exited' $LogPath
            return $false
        }
        $Process.Refresh()
        if ($Process.MainWindowHandle -ne [IntPtr]::Zero) { $hwnd = $Process.MainWindowHandle }
        if (-not (Test-AcDriveClientReady -WindowHandle $hwnd)) {
            Start-Sleep -Milliseconds 400
            continue
        }
        Send-AcResumeDrive -WindowHandle $hwnd -MgmtPort $MgmtPort -LogPath $LogPath
        if (Wait-AcDriveEngagedPoll -MgmtPort $MgmtPort -TimeoutMs 1500 -PollMs 200 -RequireControlsLive -LogPath $LogPath) {
            Write-AiSessionLog ('Drive confirmed via plugin wheel click round=' + $round) $LogPath
            if ($PresentationMode) {
                Write-AiSessionLog 'presentation camera handled by plugin acUpdate' $LogPath
            }
            return $true
        }
        if ($Process.HasExited) {
            Write-AiSessionLog 'Drive input not delivered: AC process exited' $LogPath
            return $false
        }
        [void](Invoke-AcDriveFixedClick -WindowHandle $hwnd -Process $Process -MgmtPort $MgmtPort -TimeoutMs 1200 -PollMs 400 -LogPath $LogPath)
        if (Wait-AcDriveEngagedPoll -MgmtPort $MgmtPort -TimeoutMs 1200 -PollMs 200 -RequireControlsLive -LogPath $LogPath) {
            Write-AiSessionLog ('Drive confirmed via plugin wheel click round=' + $round) $LogPath
            if ($PresentationMode) {
                Write-AiSessionLog 'presentation camera handled by plugin acUpdate' $LogPath
            }
            return $true
        }
        Start-Sleep -Milliseconds 800
    }
    Write-AiSessionLog 'Drive input not delivered: drive not confirmed (plugin wheel click timeout)' $LogPath
    return $false
}

function Wait-AcMainWindowReady {
    param(
        [Parameter(Mandatory)]$Process,
        [int]$TimeoutSec = 120,
        [string]$LogPath = ''
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $lastLog = (Get-Date).AddSeconds(-5)
    Initialize-AcSendInputNative
    while ((Get-Date) -lt $deadline) {
        try {
            if ($Process.HasExited) { return [IntPtr]::Zero }
            $Process.Refresh()
            $hwnd = $Process.MainWindowHandle
            if (($hwnd -ne [IntPtr]::Zero) -and [AcAiSendInput]::IsWindowVisible($hwnd)) {
                return $hwnd
            }
        } catch {
        }
        if ($LogPath -and ((Get-Date) - $lastLog).TotalSeconds -ge 5) {
            Write-AiSessionLog 'Drive: fenetre AC pas encore visible, attente...' $LogPath
            $lastLog = Get-Date
        }
        Start-Sleep -Milliseconds 500
    }
    return [IntPtr]::Zero
}

function Set-AcWindowForeground {
    param([IntPtr]$WindowHandle)
    return (Focus-WindowWithoutAlt -WindowHandle $WindowHandle)
}

function Start-AcsDirect {
    param(
        [Parameter(Mandatory)][string]$AiRoot,
        [string]$LogPath
    )
    $acsExe = Join-Path $AiRoot 'acs.exe'
    if (-not (Test-Path -LiteralPath $acsExe)) {
        throw ('acs.exe introuvable: ' + $acsExe)
    }
    Ensure-AcSteamAppIdFile -AiRoot $AiRoot -LogPath $LogPath
    Release-StuckModifierKeys
    $proc = Start-Process -FilePath $acsExe -WorkingDirectory $AiRoot -PassThru
    Write-AiSessionLog ('AC direct lance pid=' + $proc.Id) $LogPath
    return $proc
}

function Test-AcsProcessIsFromAiInstall {
    param(
        [Parameter(Mandatory)]$Process,
        [Parameter(Mandatory)][string]$AiRoot
    )
    if (-not $Process -or $Process.HasExited) { return $false }
    try {
        $expected = (Join-Path $AiRoot 'acs.exe').ToLowerInvariant()
        $Process.Refresh()
        return ($Process.Path.ToLowerInvariant() -eq $expected)
    } catch {
        return $false
    }
}

function Invoke-AcManagementCommand {
    param(
        [Parameter(Mandatory)][string]$Command,
        [int]$Port = 2347,
        [int]$TimeoutMs = 8000
    )
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $null }
        $client.EndConnect($iar)
        $stream = $client.GetStream()
        $cmdBytes = [System.Text.Encoding]::UTF8.GetBytes($Command)
        $stream.Write($cmdBytes, 0, $cmdBytes.Length)
        $stream.Flush()
        $ms = New-Object System.IO.MemoryStream
        $buf = New-Object byte[] 65536
        $stream.ReadTimeout = [Math]::Max(500, [int]($TimeoutMs / 4))
        $expectResponse = $Command -like 'get_*'
        $idleDeadline = [DateTime]::UtcNow.AddMilliseconds($(if ($expectResponse) { 600 } else { 120 }))
        while ([DateTime]::UtcNow -lt $idleDeadline) {
            if (-not $stream.DataAvailable) {
                if ($ms.Length -gt 0) { break }
                Start-Sleep -Milliseconds 40
                continue
            }
            $n = $stream.Read($buf, 0, $buf.Length)
            if ($n -le 0) { break }
            $ms.Write($buf, 0, $n) | Out-Null
            $idleDeadline = [DateTime]::UtcNow.AddMilliseconds(300)
        }
        if ($ms.Length -eq 0) { return $null }
        return [System.Text.Encoding]::UTF8.GetString($ms.ToArray())
    } catch {
        return $null
    } finally {
        $client.Close()
    }
}

function Get-AcStaticInfo {
    param([int]$Port = 2347)
    $raw = Invoke-AcManagementCommand -Command 'get_static_info' -Port $Port
    if (-not $raw) { return $null }
    try {
        return ($raw | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Test-AcStaticInfoCarTrack {
    param(
        $StaticInfo,
        [string]$ExpectedCar = 'lotus_exos_125',
        [string]$ExpectedTrack = 'monza'
    )
    if (-not $StaticInfo) { return $false }
    $car = [string]$StaticInfo.CarName
    $track = [string]$StaticInfo.TrackName
    if ([string]::IsNullOrWhiteSpace($car) -or [string]::IsNullOrWhiteSpace($track)) { return $false }
    return ($car.IndexOf($ExpectedCar, [StringComparison]::OrdinalIgnoreCase) -ge 0) `
        -and ($track.IndexOf($ExpectedTrack, [StringComparison]::OrdinalIgnoreCase) -ge 0)
}

function Test-AcSessionReadyForTraining {
    param(
        [Parameter(Mandatory)][string]$AiRoot,
        [Parameter(Mandatory)]$AcsProcess,
        [string]$RepoRoot,
        [string]$ExpectedCar = 'lotus_exos_125',
        [string]$ExpectedTrack = 'monza',
        [ref]$PortOpen,
        [ref]$StaticOk
    )
    $PortOpen.Value = $false
    $StaticOk.Value = $false
    if (-not (Test-AcsProcessIsFromAiInstall -Process $AcsProcess -AiRoot $AiRoot)) { return $false }
    if (-not (Test-TcpPortOpen -Port 2347)) { return $false }
    $PortOpen.Value = $true
    if (Test-TrainingAlreadyRunning -RepoRoot $RepoRoot) { return $false }
    $info = Get-AcStaticInfo -Port 2347
    if (-not (Test-AcStaticInfoCarTrack -StaticInfo $info -ExpectedCar $ExpectedCar -ExpectedTrack $ExpectedTrack)) {
        return $false
    }
    $StaticOk.Value = $true
    return $true
}

function Stop-AcsSessionProcess {
    param(
        $Process,
        [string]$LogPath
    )
    if (-not $Process) { return }
    try {
        if (-not $Process.HasExited) {
            Write-AiSessionLog ('Fermeture AC session pid=' + $Process.Id) $LogPath
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        }
    } catch {
    }
}

function Show-AcSessionFailure {
    param(
        [Parameter(Mandatory)][string]$AiRoot,
        [string]$LogPath,
        [Parameter(Mandatory)][string]$Reason,
        [bool]$PortOpen = $false,
        [bool]$StaticOk = $false,
        $Process
    )
    Write-AiSessionLog ('ERREUR: ' + $Reason) $LogPath
    if (-not $PortOpen) { Write-AiSessionLog 'port 2347 ferme' $LogPath }
    if (-not $StaticOk) { Write-AiSessionLog 'static_info echoue' $LogPath }
    $tail = Get-BootstrapLogTail -AiRoot $AiRoot
    Write-Host ''
    Write-Host '=== sensors_par_bootstrap.log (tail) ==='
    Write-Host $tail
    Stop-AcsSessionProcess -Process $Process -LogPath $LogPath
}

function Get-ContentManagerProcess {
    $procs = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessName -eq 'Content Manager' -or $_.ProcessName -eq 'ContentManager'
    })
    if ((Get-CollectionCount $procs) -eq 0) { return $null }
    return $procs[0]
}

function Initialize-AcUiAutomationAssemblies {
    if ($script:AcUiAutomationLoaded) { return }
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $script:AcUiAutomationLoaded = $true
}

function Initialize-AcUiMouseNative {
    if ($script:AcUiMouseLoaded) { return }
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public class AcAiMouse {
    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")]
    public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, int dwExtraInfo);
    public const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    public const uint MOUSEEVENTF_LEFTUP = 0x0004;
    public static void ClickCenter(int x, int y) {
        SetCursorPos(x, y);
        mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0);
        mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0);
    }
}
'@
    $script:AcUiMouseLoaded = $true
}

function Test-CmUiRectValid {
    param($Rect)
    if ($null -eq $Rect) { return $false }
    if ($Rect.Width -le 0 -or $Rect.Height -le 0) { return $false }
    if ($Rect.X -le -30000 -or $Rect.Y -le -30000) { return $false }
    return $true
}

function Test-CmUiWindowVisible {
    param($WindowElement)
    try {
        if ($WindowElement.Current.IsOffscreen) { return $false }
        $rect = $WindowElement.Current.BoundingRectangle
        if (-not (Test-CmUiRectValid -Rect $rect)) { return $false }
        if ($rect.Width -le 500 -or $rect.Height -le 400) { return $false }
        return $true
    } catch {
        return $false
    }
}

function Test-CmUiElementVisible {
    param($Element)
    try {
        if ($Element.Current.IsOffscreen) { return $false }
        return (Test-CmUiRectValid -Rect $Element.Current.BoundingRectangle)
    } catch {
        return $false
    }
}

function Get-CmTopLevelUiWindows {
    param([int]$ProcessId)
    Initialize-AcUiAutomationAssemblies
    $pidCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $ProcessId)
    return @(
        [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
            [System.Windows.Automation.TreeScope]::Children, $pidCond)
    )
}

function Get-CmWindowHandle {
    param($WindowElement)
    try {
        return [int]$WindowElement.Current.NativeWindowHandle
    } catch {
        return 0
    }
}

function Get-VisibleContentManagerWindow {
    param(
        [Parameter(Mandatory)]$CmProcess,
        [string]$LogPath
    )
    Initialize-AcUiAutomationAssemblies
    Initialize-AcSendInputNative

    if ($CmProcess.MainWindowHandle -ne [IntPtr]::Zero) {
        $hwnd = $CmProcess.MainWindowHandle
        [AcAiSendInput]::FocusWindowNoKeyboard($hwnd) | Out-Null
        Start-Sleep -Milliseconds 600
        $CmProcess.Refresh()
        if ($CmProcess.MainWindowHandle -ne [IntPtr]::Zero) {
            $fromHandle = [System.Windows.Automation.AutomationElement]::FromHandle($CmProcess.MainWindowHandle)
            if ($fromHandle -and (Test-CmUiWindowVisible -WindowElement $fromHandle)) {
                if ($LogPath) {
                    Write-AiSessionLog ('CM fenetre choisie: MainWindowHandle=' + $CmProcess.MainWindowHandle) $LogPath
                }
                return $fromHandle
            }
        }
    }

    $visible = @()
    foreach ($win in @(Get-CmTopLevelUiWindows -ProcessId $CmProcess.Id)) {
        if (Test-CmUiWindowVisible -WindowElement $win) { $visible += $win }
    }
    if ((Get-CollectionCount $visible) -eq 0) { return $null }

    $cmNamed = @($visible | Where-Object {
        $n = [string]$_.Current.Name
        ($n.IndexOf('Content Manager', [StringComparison]::OrdinalIgnoreCase) -ge 0) `
            -or ($n.IndexOf('ContentManager', [StringComparison]::OrdinalIgnoreCase) -ge 0)
    })
    $pool = if ((Get-CollectionCount $cmNamed) -gt 0) { $cmNamed } else { $visible }
    $chosen = @($pool | Sort-Object {
        $r = $_.Current.BoundingRectangle
        $r.Width * $r.Height
    } -Descending | Select-Object -First 1)[0]

    if ($LogPath -and $chosen) {
        Write-AiSessionLog (
            'CM fenetre choisie: Name=' + [string]$chosen.Current.Name `
            + ' Handle=' + (Get-CmWindowHandle -WindowElement $chosen)
        ) $LogPath
    }
    return $chosen
}

function Get-CmExcludedButtonNames {
    return @(
        'DRIVE', 'LAP TIMES', 'RESULTS', 'MEDIA', 'CONTENT', 'SETTINGS', 'ABOUT'
    )
}

function Test-CmAllowedGoButtonName {
    param([string]$Name)
    $allowed = @('Go!', 'Go', 'Lancer', 'Démarrer', 'Demarrer', 'Start')
    foreach ($a in $allowed) {
        if ($Name -ceq $a) { return $true }
    }
    return $false
}

function Test-CmExcludedButtonName {
    param([string]$Name)
    $upper = $Name.ToUpperInvariant()
    foreach ($ex in @(Get-CmExcludedButtonNames)) {
        if ($upper -eq $ex) { return $true }
    }
    return $false
}

function Get-CmGoControlTypes {
    return @(
        [System.Windows.Automation.ControlType]::Button,
        [System.Windows.Automation.ControlType]::Custom,
        [System.Windows.Automation.ControlType]::Text,
        [System.Windows.Automation.ControlType]::Hyperlink
    )
}

function New-CmUiElementRow {
    param(
        $Element,
        $RootWindow
    )
    $rect = $Element.Current.BoundingRectangle
    $cx = $rect.X + ($rect.Width / 2.0)
    $cy = $rect.Y + ($rect.Height / 2.0)
    return [pscustomobject]@{
        Name              = [string]$Element.Current.Name
        AutomationId      = [string]$Element.Current.AutomationId
        ControlType       = [string]$Element.Current.ControlType.ProgrammaticName
        IsEnabled         = [bool]$Element.Current.IsEnabled
        IsOffscreen       = [bool]$Element.Current.IsOffscreen
        BoundingRectangle = [string]$rect
        CenterX           = [math]::Round($cx, 1)
        CenterY           = [math]::Round($cy, 1)
        Element           = $Element
        Window            = $RootWindow
    }
}

function Get-CmUiElementsFromWindow {
    param(
        [Parameter(Mandatory)]$RootWindow,
        [switch]$BottomRightOnly,
        [double]$RightFraction = 0.35,
        [double]$BottomFraction = 0.35
    )
    Initialize-AcUiAutomationAssemblies
    $winRect = $RootWindow.Current.BoundingRectangle
    $minX = $winRect.X + ($winRect.Width * (1.0 - $RightFraction))
    $minY = $winRect.Y + ($winRect.Height * (1.0 - $BottomFraction))
    $rows = @()
    foreach ($ct in @(Get-CmGoControlTypes)) {
        $cond = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty, $ct)
        foreach ($el in @($RootWindow.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond))) {
            if (-not (Test-CmUiElementVisible -Element $el)) { continue }
            $row = New-CmUiElementRow -Element $el -RootWindow $RootWindow
            if ($BottomRightOnly) {
                if ($row.CenterX -lt $minX -or $row.CenterY -lt $minY) { continue }
            }
            $rows += $row
        }
    }
    return $rows
}

function Test-CmGoButtonCandidate {
    param(
        $ButtonRow,
        [double]$RightFraction = 0.35,
        [double]$BottomFraction = 0.35
    )
    if (-not $ButtonRow.IsEnabled) { return $false }
    if ($ButtonRow.IsOffscreen) { return $false }
    if (Test-CmExcludedButtonName -Name $ButtonRow.Name) { return $false }
    if (-not (Test-CmAllowedGoButtonName -Name $ButtonRow.Name)) { return $false }
    $winRect = $ButtonRow.Window.Current.BoundingRectangle
    $minX = $winRect.X + ($winRect.Width * (1.0 - $RightFraction))
    $minY = $winRect.Y + ($winRect.Height * (1.0 - $BottomFraction))
    if ($ButtonRow.CenterX -lt $minX) { return $false }
    if ($ButtonRow.CenterY -lt $minY) { return $false }
    return $true
}

function Invoke-CmUiElementActivate {
    param($Element)
    Initialize-AcUiAutomationAssemblies
    $walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
    $current = $Element
    while ($null -ne $current) {
        try {
            $pattern = $current.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
            if ($pattern) {
                $pattern.Invoke()
                return 'InvokePattern'
            }
        } catch {
        }
        try {
            $legacy = $current.GetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern)
            if ($legacy) {
                $action = [string]$legacy.Current.DefaultAction
                if (-not [string]::IsNullOrWhiteSpace($action)) {
                    $legacy.DoDefaultAction()
                    return 'LegacyIAccessible'
                }
            }
        } catch {
        }
        $parent = $walker.GetParent($current)
        if ($null -eq $parent -or $parent -eq [System.Windows.Automation.AutomationElement]::RootElement) {
            break
        }
        $current = $parent
    }
    if (-not (Test-CmUiElementVisible -Element $Element)) {
        throw 'Element Go CM hors ecran ou rectangle invalide.'
    }
    Initialize-AcUiMouseNative
    $rect = $Element.Current.BoundingRectangle
    $cx = [int][math]::Round($rect.X + ($rect.Width / 2.0))
    $cy = [int][math]::Round($rect.Y + ($rect.Height / 2.0))
    [AcAiMouse]::ClickCenter($cx, $cy) | Out-Null
    return 'BoundingRectangle'
}

function Get-CmTopLevelWindowRows {
    param([int]$ProcessId)
    $rows = @()
    foreach ($win in @(Get-CmTopLevelUiWindows -ProcessId $ProcessId)) {
        $rect = $win.Current.BoundingRectangle
        $rows += [pscustomobject]@{
            Name        = [string]$win.Current.Name
            Handle      = Get-CmWindowHandle -WindowElement $win
            IsOffscreen = [bool]$win.Current.IsOffscreen
            Rect        = [string]$rect
            Visible     = (Test-CmUiWindowVisible -WindowElement $win)
            Element     = $win
        }
    }
    return $rows
}

function Show-ContentManagerUiDiagnostic {
    param([string]$LogPath)
    $cm = Get-ContentManagerProcess
    if (-not $cm) {
        throw 'Content Manager non detecte pour diagnostic UIA.'
    }

    Write-Host ('Content Manager pid=' + $cm.Id)
    Write-Host '=== Fenetres premier niveau ==='
    $topRows = @(Get-CmTopLevelWindowRows -ProcessId $cm.Id)
    foreach ($w in $topRows) {
        $line = '  Window Name=' + $w.Name + ' Handle=' + $w.Handle `
            + ' IsOffscreen=' + $w.IsOffscreen + ' Visible=' + $w.Visible + ' Rect=' + $w.Rect
        Write-Host $line
        if ($LogPath) { Write-AiSessionLog $line.Trim() $LogPath }
    }

    $root = Get-VisibleContentManagerWindow -CmProcess $cm -LogPath $LogPath
    if (-not $root) {
        throw 'Aucune fenetre Content Manager visible (UIA).'
    }
    $chosenLine = '=== Fenetre choisie: Name=' + [string]$root.Current.Name `
        + ' Handle=' + (Get-CmWindowHandle -WindowElement $root) `
        + ' Rect=' + [string]$root.Current.BoundingRectangle
    Write-Host $chosenLine
    if ($LogPath) { Write-AiSessionLog $chosenLine $LogPath }

    Write-Host '=== Elements quart inferieur droit ==='
    $elements = @(Get-CmUiElementsFromWindow -RootWindow $root -BottomRightOnly)
    Write-Host ('  count=' + (Get-CollectionCount $elements))
    foreach ($el in $elements) {
        $line = '  Name=' + $el.Name + ' ControlType=' + $el.ControlType `
            + ' AutomationId=' + $el.AutomationId + ' IsEnabled=' + $el.IsEnabled `
            + ' IsOffscreen=' + $el.IsOffscreen + ' Rect=' + $el.BoundingRectangle
        Write-Host $line
        if ($LogPath) { Write-AiSessionLog $line.Trim() $LogPath }
    }
}

function Get-AcsProcessIdsFromAiInstall {
    param([string]$AiRoot)
    $expected = (Join-Path $AiRoot 'acs.exe').ToLowerInvariant()
    $ids = @()
    foreach ($proc in @(Get-Process -Name 'acs' -ErrorAction SilentlyContinue)) {
        try {
            if ($proc.Path.ToLowerInvariant() -eq $expected) { $ids += $proc.Id }
        } catch {
        }
    }
    return @($ids)
}

function Wait-AcsProcessAfterCmGo {
    param(
        [Parameter(Mandatory)][string]$AiRoot,
        [int[]]$KnownPids = @(),
        [int]$TimeoutSec = 30
    )
    $expected = (Join-Path $AiRoot 'acs.exe').ToLowerInvariant()
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        foreach ($proc in @(Get-Process -Name 'acs' -ErrorAction SilentlyContinue)) {
            if ($KnownPids -contains $proc.Id) { continue }
            try {
                if ($proc.Path.ToLowerInvariant() -eq $expected) { return $proc }
            } catch {
            }
        }
        Start-Sleep -Milliseconds 400
    }
    return $null
}

function Invoke-ContentManagerGoButton {
    param(
        [Parameter(Mandatory)][string]$AiRoot,
        [string]$LogPath,
        [int]$SearchTimeoutSec = 90,
        [int]$AcsStartTimeoutSec = 30
    )
    Initialize-AcUiAutomationAssemblies

    $cm = $null
    $deadline = (Get-Date).AddSeconds($SearchTimeoutSec)
    while ((Get-Date) -lt $deadline -and -not $cm) {
        $cm = Get-ContentManagerProcess
        Start-Sleep -Seconds 1
    }
    if (-not $cm) {
        throw 'Content Manager non detecte pour automation Go.'
    }

    $knownAcsPids = @(Get-AcsProcessIdsFromAiInstall -AiRoot $AiRoot)
    $deadline = (Get-Date).AddSeconds($SearchTimeoutSec)
    $allCandidates = @()

    while ((Get-Date) -lt $deadline) {
        $root = Get-VisibleContentManagerWindow -CmProcess $cm -LogPath $LogPath
        if (-not $root) {
            Start-Sleep -Seconds 1
            continue
        }

        $matches = @()
        foreach ($row in @(Get-CmUiElementsFromWindow -RootWindow $root)) {
            if (Test-CmGoButtonCandidate -ButtonRow $row) {
                $matches += $row
            }
        }
        $allCandidates += $matches

        if ((Get-CollectionCount $matches) -gt 0) {
            $goBang = @($matches | Where-Object { $_.Name -ceq 'Go!' })
            if ((Get-CollectionCount $goBang) -gt 0) {
                $chosen = @($goBang | Sort-Object CenterY, CenterX | Select-Object -Last 1)[0]
            } else {
                $chosen = @($matches | Sort-Object CenterY, CenterX | Select-Object -Last 1)[0]
            }

            Write-AiSessionLog (
                'CM candidate: Name=' + $chosen.Name + ' Type=' + $chosen.ControlType `
                + ' X=' + $chosen.CenterX + ' Y=' + $chosen.CenterY
            ) $LogPath

            $method = Invoke-CmUiElementActivate -Element $chosen.Element
            Write-AiSessionLog ('CM Go invoque: ' + $chosen.Name + ' via ' + $method) $LogPath

            $acs = Wait-AcsProcessAfterCmGo -AiRoot $AiRoot -KnownPids $knownAcsPids -TimeoutSec $AcsStartTimeoutSec
            if (-not $acs) {
                Write-Host 'Candidats UIA Go (apres clic):'
                foreach ($c in @($allCandidates | Sort-Object CenterY, CenterX -Unique)) {
                    Write-Host ('  ' + $c.Name + ' ' + $c.ControlType + ' X=' + $c.CenterX + ' Y=' + $c.CenterY)
                }
                throw 'Clic Go CM echoue: aucun acs.exe AI demarre dans les 30 secondes.'
            }
            Write-AiSessionLog ('acs.exe AI demarre pid=' + $acs.Id) $LogPath
            return $acs
        }
        Start-Sleep -Seconds 1
    }

    Write-Host 'Elements UIA quart inferieur droit (candidats):'
    $root = Get-VisibleContentManagerWindow -CmProcess $cm -LogPath $LogPath
    if ($root) {
        foreach ($el in @(Get-CmUiElementsFromWindow -RootWindow $root -BottomRightOnly)) {
            Write-Host ('  ' + $el.Name + ' ' + $el.ControlType + ' X=' + $el.CenterX + ' Y=' + $el.CenterY)
        }
    }
    if ($LogPath) {
        Write-AiSessionLog 'Bouton Go! introuvable dans fenetre CM visible' $LogPath
    }
    throw 'Bouton Go! introuvable dans Content Manager (fenetre visible, quart inferieur droit).'
}

function Start-AiSessionViaContentManager {
    param(
        [Parameter(Mandatory)][string]$ScriptDir,
        [Parameter(Mandatory)][string]$AiRoot,
        [string]$LogPath,
        [int]$AcsTimeoutSec = 600
    )
    Invoke-RequiredPsScript -Path (Join-Path $ScriptDir 'Start-ContentManager-AI.ps1') `
        -Parameters @{ SkipProfileSwitch = $true } `
        -ErrorMessage 'Start-ContentManager-AI.ps1 a echoue.'
    Start-Sleep -Seconds 3
    $acs = Invoke-ContentManagerGoButton -AiRoot $AiRoot -LogPath $LogPath -AcsStartTimeoutSec 30
    if (-not $acs) {
        throw 'acs.exe copie AI non detecte apres Go Content Manager.'
    }
    return $acs
}

function Initialize-AcWindowPlacementNative {
    if ($script:AcWindowPlacementNativeReady) { return $true }
    if (([System.Management.Automation.PSTypeName]'AcWindowPlacementNative').Type) {
        $script:AcWindowPlacementNativeReady = $true
        return $true
    }
    try {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public struct AcRect {
    public int Left;
    public int Top;
    public int Right;
    public int Bottom;
    public int Width { get { return Right - Left; } }
    public int Height { get { return Bottom - Top; } }
}

public static class AcWindowPlacementNative {
    public const uint SWP_NOACTIVATE = 0x0010;
    public const uint SWP_SHOWWINDOW = 0x0040;
    public const uint WS_OVERLAPPEDWINDOW = 0x00CF0000;
    public static readonly IntPtr HWND_TOP = IntPtr.Zero;

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool GetWindowRect(IntPtr hWnd, out AcRect lpRect);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int nWidth, int nHeight, bool bRepaint);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool SetWindowText(IntPtr hWnd, string lpString);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool AdjustWindowRectEx(ref AcRect lpRect, uint dwStyle, bool bMenu, uint dwExStyle);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    public static AcRect ClientToOuter(int clientWidth, int clientHeight) {
        AcRect rect = new AcRect { Left = 0, Top = 0, Right = clientWidth, Bottom = clientHeight };
        AdjustWindowRectEx(ref rect, WS_OVERLAPPEDWINDOW, false, 0);
        return new AcRect {
            Left = 0,
            Top = 0,
            Right = rect.Right - rect.Left,
            Bottom = rect.Bottom - rect.Top
        };
    }

    public static AcRect OuterToClient(int outerWidth, int outerHeight) {
        AcRect probe = ClientToOuter(100, 100);
        int chromeW = probe.Width - 100;
        int chromeH = probe.Height - 100;
        if (chromeW < 0) { chromeW = 0; }
        if (chromeH < 0) { chromeH = 0; }
        int clientW = Math.Max(0, outerWidth - chromeW);
        int clientH = Math.Max(0, outerHeight - chromeH);
        return new AcRect {
            Left = 0,
            Top = 0,
            Right = clientW,
            Bottom = clientH
        };
    }
}
'@ -ErrorAction Stop
        $script:AcWindowPlacementNativeReady = $true
        return $true
    } catch {
        return $false
    }
}

function Get-AcVideoIniKeyCatalog {
    return [ordered]@{
        Fullscreen   = @{ Section = 'VIDEO'; Key = 'FULLSCREEN'; Required = $true }
        Width        = @{ Section = 'VIDEO'; Key = 'WIDTH'; Required = $true }
        Height       = @{ Section = 'VIDEO'; Key = 'HEIGHT'; Required = $true }
        FpsCapMs     = @{ Section = 'VIDEO'; Key = 'FPS_CAP_MS'; Required = $false }
        MonitorIndex = @{ Section = 'VIDEO'; Key = 'INDEX'; Required = $false }
        Vsync        = @{ Section = 'VIDEO'; Key = 'VSYNC'; Required = $false }
        ShadowMap    = @{ Section = 'VIDEO'; Key = 'SHADOW_MAP_SIZE'; Required = $false }
        PostEnabled  = @{ Section = 'POST_PROCESS'; Key = 'ENABLED'; Required = $false }
        PostQuality  = @{ Section = 'POST_PROCESS'; Key = 'QUALITY'; Required = $false }
        PostGlare    = @{ Section = 'POST_PROCESS'; Key = 'GLARE'; Required = $false }
        PostDof      = @{ Section = 'POST_PROCESS'; Key = 'DOF'; Required = $false }
        PostHeat     = @{ Section = 'POST_PROCESS'; Key = 'HEAT_SHIMMER'; Required = $false }
        PostRays     = @{ Section = 'POST_PROCESS'; Key = 'RAYS_OF_GOD'; Required = $false }
        Smoke        = @{ Section = 'EFFECTS'; Key = 'SMOKE'; Required = $false }
        SmokeMirror  = @{ Section = 'EFFECTS'; Key = 'RENDER_SMOKE_IN_MIRROR'; Required = $false }
        MirrorHq     = @{ Section = 'MIRROR'; Key = 'HQ'; Required = $false }
        MirrorSize   = @{ Section = 'MIRROR'; Key = 'SIZE'; Required = $false }
    }
}

function Get-AcVideoIniKeysAudit {
    param([Parameter(Mandatory)][string]$Path)
    $catalog = Get-AcVideoIniKeyCatalog
    $found = [ordered]@{}
    foreach ($entry in $catalog.GetEnumerator()) {
        $meta = $entry.Value
        $val = Get-IniValue -Path $Path -Section $meta.Section -Key $meta.Key
        if ($null -ne $val) {
            $found[$entry.Key] = @{ Section = $meta.Section; Key = $meta.Key; Value = $val; Required = $meta.Required }
        }
    }
    return $found
}

function Get-AcWindowGridDimensions {
    param([Parameter(Mandatory)][int]$WorkerCount)
    if ($WorkerCount -lt 1 -or $WorkerCount -gt 6) {
        throw ('WorkerCount hors plage 1-6: ' + $WorkerCount)
    }
    switch ($WorkerCount) {
        1 { return @{ Cols = 1; Rows = 1 } }
        2 { return @{ Cols = 2; Rows = 1 } }
        { $_ -in 3, 4 } { return @{ Cols = 2; Rows = 2 } }
        default { return @{ Cols = 3; Rows = 2 } }
    }
}

function Get-AcEvenDimension {
    param([int]$Value)
    if ($Value -le 0) { return 0 }
    return [int]([math]::Floor($Value / 2.0) * 2)
}

function Get-AcGpuSafeDimension {
    param(
        [int]$Value,
        [int]$Align = 16,
        [int]$Minimum = 32
    )
    if ($Value -le 0) { return 0 }
    if ($Align -lt 2) { $Align = 2 }
    $aligned = [int]([math]::Floor($Value / [double]$Align) * $Align)
    if ($aligned -lt $Minimum) { $aligned = $Minimum }
    return Get-AcEvenDimension $aligned
}

function Get-AcGpuSafeRender16x9 {
    param(
        [Parameter(Mandatory)][int]$MaxClientWidth,
        [Parameter(Mandatory)][int]$MaxClientHeight
    )
    if ($MaxClientWidth -lt 32 -or $MaxClientHeight -lt 32) {
        return @{ Width = 0; Height = 0 }
    }
    $ideal = Get-AcMaxResolution16x9 -MaxWidth $MaxClientWidth -MaxHeight $MaxClientHeight
    $startW = Get-AcGpuSafeDimension $ideal.Width
    for ($w = $startW; $w -ge 32; $w -= 16) {
        $h = Get-AcGpuSafeDimension ([int][math]::Floor($w * 9.0 / 16.0))
        if ($h -lt 32) { continue }
        if ($w -gt $MaxClientWidth -or $h -gt $MaxClientHeight) { continue }
        return @{ Width = $w; Height = $h }
    }
    return @{ Width = 0; Height = 0 }
}

function Get-AcMaxResolution16x9 {
    param(
        [Parameter(Mandatory)][int]$MaxWidth,
        [Parameter(Mandatory)][int]$MaxHeight
    )
    if ($MaxWidth -lt 1 -or $MaxHeight -lt 1) {
        return @{ Width = 0; Height = 0 }
    }
    $w = $MaxWidth
    $h = [int][math]::Floor($w * 9.0 / 16.0)
    if ($h -gt $MaxHeight) {
        $h = $MaxHeight
        $w = [int][math]::Floor($h * 16.0 / 9.0)
    }
    $w = Get-AcEvenDimension $w
    $h = Get-AcEvenDimension $h
    if ($w -lt 2) { $w = 2 }
    if ($h -lt 2) { $h = 2 }
    return @{ Width = $w; Height = $h }
}

function Get-AcRenderSizeForCell {
    param(
        [Parameter(Mandatory)][int]$CellOuterWidth,
        [Parameter(Mandatory)][int]$CellOuterHeight,
        [int]$CellBaseWidth = 0,
        [int]$CellBaseHeight = 0,
        [int]$PreferredClientWidth = 0,
        [int]$PreferredClientHeight = 0,
        [int]$MaxRenderWidth = 0,
        [int]$MaxRenderHeight = 0
    )
    $maxOuterW = $CellOuterWidth
    $maxOuterH = $CellOuterHeight
    if ($PreferredClientWidth -gt 0 -and $PreferredClientHeight -gt 0) {
        $prefW = $PreferredClientWidth
        $prefH = $PreferredClientHeight
        if ($MaxRenderWidth -gt 0) { $prefW = [Math]::Min($prefW, $MaxRenderWidth) }
        if ($MaxRenderHeight -gt 0) { $prefH = [Math]::Min($prefH, $MaxRenderHeight) }
        $gpu = Get-AcGpuSafeRender16x9 -MaxClientWidth $prefW -MaxClientHeight $prefH
        $renderW = $gpu.Width
        $renderH = $gpu.Height
    } elseif (Initialize-AcWindowPlacementNative) {
        $clientMax = [AcWindowPlacementNative]::OuterToClient($maxOuterW, $maxOuterH)
        $maxClientW = $clientMax.Width
        $maxClientH = $clientMax.Height
        if ($MaxRenderWidth -gt 0) { $maxClientW = [Math]::Min($maxClientW, $MaxRenderWidth) }
        if ($MaxRenderHeight -gt 0) { $maxClientH = [Math]::Min($maxClientH, $MaxRenderHeight) }
        $gpu = Get-AcGpuSafeRender16x9 -MaxClientWidth $maxClientW -MaxClientHeight $maxClientH
        $renderW = $gpu.Width
        $renderH = $gpu.Height
    } else {
        $baseW = if ($CellBaseWidth -gt 0) { $CellBaseWidth } else { $CellOuterWidth }
        $baseH = if ($CellBaseHeight -gt 0) { $CellBaseHeight } else { $CellOuterHeight }
        if ($MaxRenderWidth -gt 0) { $baseW = [Math]::Min($baseW, $MaxRenderWidth) }
        if ($MaxRenderHeight -gt 0) { $baseH = [Math]::Min($baseH, $MaxRenderHeight) }
        $ideal = Get-AcMaxResolution16x9 -MaxWidth $baseW -MaxHeight $baseH
        $renderW = $ideal.Width
        $renderH = $ideal.Height
    }
    $outerW = $maxOuterW
    $outerH = $maxOuterH
    if (Initialize-AcWindowPlacementNative) {
        $outer = [AcWindowPlacementNative]::ClientToOuter($renderW, $renderH)
        $outerW = [Math]::Min($maxOuterW, $outer.Width)
        $outerH = [Math]::Min($maxOuterH, $outer.Height)
        $fitClient = [AcWindowPlacementNative]::OuterToClient($outerW, $outerH)
        if ($fitClient.Width -lt $renderW -or $fitClient.Height -lt $renderH) {
            $gpuFit = Get-AcGpuSafeRender16x9 -MaxClientWidth $fitClient.Width -MaxClientHeight $fitClient.Height
            $renderW = $gpuFit.Width
            $renderH = $gpuFit.Height
            $outer = [AcWindowPlacementNative]::ClientToOuter($renderW, $renderH)
            $outerW = [Math]::Min($maxOuterW, $outer.Width)
            $outerH = [Math]::Min($maxOuterH, $outer.Height)
        }
    }
    $outerX = [int][math]::Floor(($CellOuterWidth - $outerW) / 2.0)
    $outerY = [int][math]::Floor(($CellOuterHeight - $outerH) / 2.0)
    return @{
        renderWidth  = $renderW
        renderHeight = $renderH
        outerWidth   = $outerW
        outerHeight  = $outerH
        outerX       = $outerX
        outerY       = $outerY
    }
}

function Get-AcMonitorWorkingArea {
    param([int]$MonitorIndex = 0)
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
    $screens = [System.Windows.Forms.Screen]::AllScreens
    if ($MonitorIndex -lt 0 -or $MonitorIndex -ge $screens.Count) {
        throw ('MonitorIndex ' + $MonitorIndex + ' invalide (' + $screens.Count + ' moniteur(s)).')
    }
    $area = $screens[$MonitorIndex].WorkingArea
    return @{
        Index  = $MonitorIndex
        X      = [int]$area.X
        Y      = [int]$area.Y
        Width  = [int]$area.Width
        Height = [int]$area.Height
    }
}

function Get-AcWindowGridLayout {
    param(
        [Parameter(Mandatory)][int]$WorkerCount,
        [int]$MonitorIndex = 0,
        [int]$WindowGap = 4,
        [ValidateSet('Auto', 'Grid', 'Legacy')]
        [string]$WindowLayout = 'Auto',
        [int]$WindowWidth = 0,
        [int]$WindowHeight = 0,
        [hashtable]$WorkingAreaOverride = $null
    )
    $grid = Get-AcWindowGridDimensions -WorkerCount $WorkerCount
    $cols = $grid.Cols
    $rows = $grid.Rows
    $wa = if ($WorkingAreaOverride) { $WorkingAreaOverride } else { Get-AcMonitorWorkingArea -MonitorIndex $MonitorIndex }

    $cellBaseW = [int][math]::Floor($wa.Width / $cols)
    $cellBaseH = [int][math]::Floor($wa.Height / $rows)
    $legacyCap = ($WindowLayout -eq 'Legacy')
    $capW = if ($legacyCap) { 1280 } else { 0 }
    $capH = if ($legacyCap) { 720 } else { 0 }
    $cells = @()
    for ($i = 0; $i -lt $WorkerCount; $i++) {
        $col = $i % $cols
        $row = [int][math]::Floor($i / $cols)
        $screenW = [int][math]::Floor(($wa.Width - ($cols - 1) * $WindowGap) / $cols)
        $screenH = [int][math]::Floor(($wa.Height - ($rows - 1) * $WindowGap) / $rows)
        $screenX = $wa.X + $col * ($screenW + $WindowGap)
        $screenY = $wa.Y + $row * ($screenH + $WindowGap)
        if ($WindowWidth -gt 0 -and $WindowHeight -gt 0) {
            $size = Get-AcRenderSizeForCell -CellOuterWidth $screenW -CellOuterHeight $screenH `
                -CellBaseWidth $cellBaseW -CellBaseHeight $cellBaseH `
                -PreferredClientWidth $WindowWidth -PreferredClientHeight $WindowHeight `
                -MaxRenderWidth $capW -MaxRenderHeight $capH
        } elseif ($legacyCap -and $WorkerCount -le 2) {
            $size = Get-AcRenderSizeForCell -CellOuterWidth $screenW -CellOuterHeight $screenH `
                -CellBaseWidth $cellBaseW -CellBaseHeight $cellBaseH `
                -PreferredClientWidth $capW -PreferredClientHeight $capH `
                -MaxRenderWidth $capW -MaxRenderHeight $capH
        } else {
            $size = Get-AcRenderSizeForCell -CellOuterWidth $screenW -CellOuterHeight $screenH `
                -CellBaseWidth $cellBaseW -CellBaseHeight $cellBaseH `
                -MaxRenderWidth $capW -MaxRenderHeight $capH
        }
        $outerX = $screenX + $size.outerX
        $outerY = $screenY + $size.outerY
        $cells += [ordered]@{
            workerId       = $i
            col            = $col
            row            = $row
            screenX        = $screenX
            screenY        = $screenY
            screenWidth    = $screenW
            screenHeight   = $screenH
            outerX         = $outerX
            outerY         = $outerY
            outerWidth     = $size.outerWidth
            outerHeight    = $size.outerHeight
            renderWidth    = $size.renderWidth
            renderHeight   = $size.renderHeight
        }
    }
    return [ordered]@{
        layout       = $WindowLayout
        monitorIndex = $MonitorIndex
        windowGap    = $WindowGap
        cols         = $cols
        rows         = $rows
        workingArea  = $wa
        cells        = @($cells)
    }
}

function Set-AcIniValueWithOrigBackup {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Section,
        [Parameter(Mandatory)][string]$Key,
        [Parameter(Mandatory)][string]$Value
    )
    $origKey = '__ORIG_' + $Key
    $current = Get-IniValue -Path $Path -Section $Section -Key $Key
    if ($null -eq $current) { return $false }
    $existingOrig = Get-IniValue -Path $Path -Section $Section -Key $origKey
    if ($null -eq $existingOrig) {
        Set-IniValue -Path $Path -Section $Section -Key $origKey -Value $current
    }
    Set-IniValue -Path $Path -Section $Section -Key $Key -Value $Value
    return $true
}

function Backup-AcWorkerVideoIni {
    param(
        [Parameter(Mandatory)][string]$Docs,
        [Parameter(Mandatory)][int]$WorkerId,
        [Parameter(Mandatory)][string]$BackupRoot
    )
    $live = Join-Path $Docs 'cfg\video.ini'
    if (-not (Test-Path -LiteralPath $live)) { return $null }
    $dest = Join-Path $BackupRoot ('worker_' + $WorkerId + '_video.ini')
    $parent = Split-Path -Parent $dest
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Copy-Item -LiteralPath $live -Destination $dest -Force
    return $dest
}

function Apply-AcVideoIniSixInstancesReductions {
    param([Parameter(Mandatory)][string]$VideoIniPath)
    $reductions = @(
        @{ Section = 'POST_PROCESS'; Key = 'ENABLED'; Value = '0' }
        @{ Section = 'POST_PROCESS'; Key = 'QUALITY'; Value = '0' }
        @{ Section = 'POST_PROCESS'; Key = 'GLARE'; Value = '0' }
        @{ Section = 'POST_PROCESS'; Key = 'DOF'; Value = '0' }
        @{ Section = 'POST_PROCESS'; Key = 'HEAT_SHIMMER'; Value = '0' }
        @{ Section = 'POST_PROCESS'; Key = 'RAYS_OF_GOD'; Value = '0' }
        @{ Section = 'EFFECTS'; Key = 'SMOKE'; Value = '0' }
        @{ Section = 'EFFECTS'; Key = 'RENDER_SMOKE_IN_MIRROR'; Value = '0' }
        @{ Section = 'MIRROR'; Key = 'HQ'; Value = '0' }
        @{ Section = 'MIRROR'; Key = 'SIZE'; Value = '256' }
        @{ Section = 'VIDEO'; Key = 'SHADOW_MAP_SIZE'; Value = '256' }
        @{ Section = 'VIDEO'; Key = 'AASAMPLES'; Value = '0' }
        @{ Section = 'VIDEO'; Key = 'AAQUALITY'; Value = '0' }
        @{ Section = 'VIDEO'; Key = 'ANISOTROPIC'; Value = '0' }
        @{ Section = 'VIDEO'; Key = 'VSYNC'; Value = '0' }
        @{ Section = 'ASSETTOCORSA'; Key = 'WORLD_DETAIL'; Value = '0' }
        @{ Section = 'CUBEMAP'; Key = 'SIZE'; Value = '0' }
        @{ Section = 'CUBEMAP'; Key = 'FACES_PER_FRAME'; Value = '0' }
    )
    $applied = @()
    foreach ($r in $reductions) {
        if ($null -ne (Get-IniValue -Path $VideoIniPath -Section $r.Section -Key $r.Key)) {
            Set-AcIniValueWithOrigBackup -Path $VideoIniPath -Section $r.Section -Key $r.Key -Value $r.Value | Out-Null
            $applied += ($r.Section + '.' + $r.Key)
        }
    }
    return $applied
}

function Apply-AcAiProfileVideoSixInstances {
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$BackupRoot
    )
    $aiVideo = Join-Path $RepoRoot 'ac_profiles\ai\cfg\video.ini'
    if (-not (Test-Path -LiteralPath $aiVideo)) {
        throw ('video.ini profil AI absent: ' + $aiVideo)
    }
    $bak = Join-Path $BackupRoot 'ai_profile_video.ini'
    $bakParent = Split-Path -Parent $bak
    if ($bakParent -and -not (Test-Path -LiteralPath $bakParent)) {
        New-Item -ItemType Directory -Path $bakParent -Force | Out-Null
    }
    Copy-Item -LiteralPath $aiVideo -Destination $bak -Force
    return (Apply-AcVideoIniSixInstancesReductions -VideoIniPath $aiVideo)
}

function Apply-AcWorkerVideoIniWindowed {
    param(
        [Parameter(Mandatory)][string]$Docs,
        [Parameter(Mandatory)][hashtable]$Cell,
        [int]$MonitorIndex = 0,
        [ValidateSet('Default', 'SixInstances')]
        [string]$RenderPreset = 'Default',
        [string]$LogPath = ''
    )
    $videoIni = Join-Path $Docs 'cfg\video.ini'
    if (-not (Test-Path -LiteralPath $videoIni)) {
        throw ('video.ini absent: ' + $videoIni)
    }
    $audit = Get-AcVideoIniKeysAudit -Path $videoIni
    foreach ($req in @('Fullscreen', 'Width', 'Height')) {
        if (-not $audit.Contains($req)) {
            $sections = @{}
            foreach ($item in $audit.Values) {
                if (-not $sections.ContainsKey($item.Section)) { $sections[$item.Section] = @() }
                $sections[$item.Section] += $item.Key
            }
            $detail = ($sections.GetEnumerator() | ForEach-Object { '[' + $_.Key + '] ' + ($_.Value -join ', ') }) -join '; '
            throw ('video.ini: cle requise absente (' + $req + '). Fichier=' + $videoIni + ' sections=' + $detail)
        }
    }

    $rw = [int]$Cell.renderWidth
    $rh = [int]$Cell.renderHeight
    if ($rw % 2 -ne 0) { $rw-- }
    if ($rh % 2 -ne 0) { $rh-- }
    $rw = [Math]::Max(640, $rw)
    $rh = [Math]::Max(360, $rh)
    Set-AcIniValueWithOrigBackup -Path $videoIni -Section 'VIDEO' -Key 'FULLSCREEN' -Value '0' | Out-Null
    Set-AcIniValueWithOrigBackup -Path $videoIni -Section 'VIDEO' -Key 'WIDTH' -Value ([string]$rw) | Out-Null
    Set-AcIniValueWithOrigBackup -Path $videoIni -Section 'VIDEO' -Key 'HEIGHT' -Value ([string]$rh) | Out-Null
    if ($null -ne (Get-IniValue -Path $videoIni -Section 'VIDEO' -Key 'INDEX')) {
        Set-AcIniValueWithOrigBackup -Path $videoIni -Section 'VIDEO' -Key 'INDEX' -Value ([string]$MonitorIndex) | Out-Null
    }

    $fpsApplied = $false
    if ($RenderPreset -eq 'SixInstances' -and $null -ne (Get-IniValue -Path $videoIni -Section 'VIDEO' -Key 'FPS_CAP_MS')) {
        Set-AcIniValueWithOrigBackup -Path $videoIni -Section 'VIDEO' -Key 'FPS_CAP_MS' -Value '20' | Out-Null
        $fpsApplied = $true
    }

    $sixApplied = @()
    if ($RenderPreset -eq 'SixInstances') {
        $sixApplied = @(Apply-AcVideoIniSixInstancesReductions -VideoIniPath $videoIni)
    }

    if ($LogPath) {
        Write-AiSessionLog (
            'video.ini windowed render=' + $Cell.renderWidth + 'x' + $Cell.renderHeight `
            + ' fullscreen=0 monitor=' + $MonitorIndex + ' fpsCap30=' + $fpsApplied
        ) $LogPath
        if ($sixApplied.Count -gt 0) {
            Write-AiSessionLog ('SixInstances reductions: ' + ($sixApplied -join ', ')) $LogPath
        }
    }

    return @{
        Audit       = $audit
        FpsCap30    = $fpsApplied
        SixApplied  = $sixApplied
        RenderWidth = $Cell.renderWidth
        RenderHeight = $Cell.renderHeight
    }
}

function Get-AcProcessMainWindowByPid {
    param(
        [Parameter(Mandatory)][int]$ProcessId,
        [int]$TimeoutSec = 120,
        [switch]$FailFastIfMissing
    )
    if (-not (Initialize-AcWindowPlacementNative)) { return [IntPtr]::Zero }
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if (-not $proc) {
            if ($FailFastIfMissing) { return [IntPtr]::Zero }
            Start-Sleep -Milliseconds 500
            continue
        }
        try {
            if ($proc.HasExited) { return [IntPtr]::Zero }
            $proc.Refresh()
            $hwnd = $proc.MainWindowHandle
            if (($hwnd -ne [IntPtr]::Zero) -and [AcWindowPlacementNative]::IsWindowVisible($hwnd)) {
                return $hwnd
            }
        } catch {
        }
        Start-Sleep -Milliseconds 500
    }
    return [IntPtr]::Zero
}

function Get-AcWindowRectInfo {
    param([Parameter(Mandatory)][IntPtr]$WindowHandle)
    if (-not (Initialize-AcWindowPlacementNative)) { return $null }
    if ($WindowHandle -eq [IntPtr]::Zero) { return $null }
    $rect = New-Object AcRect
    if (-not [AcWindowPlacementNative]::GetWindowRect($WindowHandle, [ref]$rect)) { return $null }
    return @{
        X      = $rect.Left
        Y      = $rect.Top
        Width  = $rect.Width
        Height = $rect.Height
    }
}

function Set-AcWorkerWindowTitle {
    param(
        [Parameter(Mandatory)][IntPtr]$WindowHandle,
        [Parameter(Mandatory)][int]$WorkerId,
        [string]$LogPath = ''
    )
    if ($WindowHandle -eq [IntPtr]::Zero) { return $false }
    if (-not (Initialize-AcWindowPlacementNative)) { return $false }
    $title = 'Assetto Corsa Worker ' + $WorkerId
    $ok = [AcWindowPlacementNative]::SetWindowText($WindowHandle, $title)
    if ($LogPath) {
        Write-AiSessionLog ('SetWindowText ok=' + $ok + ' title="' + $title + '"') $LogPath
    }
    return [bool]$ok
}

function Set-AcWorkerWindowPosition {
    param(
        [Parameter(Mandatory)][IntPtr]$WindowHandle,
        [Parameter(Mandatory)][hashtable]$Cell,
        [string]$LogPath = ''
    )
    if ($WindowHandle -eq [IntPtr]::Zero) { return $false }
    if (-not (Initialize-AcWindowPlacementNative)) {
        if ($LogPath) { Write-AiSessionLog 'window placement unavailable: native helper not loaded' $LogPath }
        return $false
    }
    $posX = if ($null -ne $Cell.outerX) { [int]$Cell.outerX } else { [int]$Cell.screenX }
    $posY = if ($null -ne $Cell.outerY) { [int]$Cell.outerY } else { [int]$Cell.screenY }
    $outerW = if ($null -ne $Cell.outerWidth) { [int]$Cell.outerWidth } else { [int]$Cell.screenWidth }
    $outerH = if ($null -ne $Cell.outerHeight) { [int]$Cell.outerHeight } else { [int]$Cell.screenHeight }
    $flags = [AcWindowPlacementNative]::SWP_NOACTIVATE -bor [AcWindowPlacementNative]::SWP_SHOWWINDOW
    $ok = [AcWindowPlacementNative]::SetWindowPos(
        $WindowHandle,
        [AcWindowPlacementNative]::HWND_TOP,
        $posX,
        $posY,
        $outerW,
        $outerH,
        $flags
    )
    if ($LogPath) {
        Write-AiSessionLog (
            'SetWindowPos SWP_NOACTIVATE ok=' + $ok + ' worker=' + $Cell.workerId `
            + ' outer=' + $posX + ',' + $posY + ' ' + $outerW + 'x' + $outerH `
            + ' render=' + $Cell.renderWidth + 'x' + $Cell.renderHeight
        ) $LogPath
    }
    return [bool]$ok
}

function Invoke-AcWorkerWindowPlacement {
    param(
        [AllowNull()]
        [Parameter(Mandatory = $false)]
        $Process,
        [Parameter(Mandatory)][hashtable]$Cell,
        [string]$LogPath = '',
        [switch]$AfterDrive
    )
    try {
        if (-not $Process -or $Process.HasExited) { return $false }
        if (-not (Initialize-AcWindowPlacementNative)) {
            if ($LogPath) { Write-AiSessionLog 'window placement unavailable' $LogPath }
            return $false
        }
        $hwnd = Get-AcProcessMainWindowByPid -ProcessId $Process.Id -TimeoutSec 120
        if ($hwnd -eq [IntPtr]::Zero) {
            if ($LogPath) {
                Write-AiSessionLog (
                    'fenetre acs.exe introuvable pid=' + $Process.Id + ' afterDrive=' + [bool]$AfterDrive
                ) $LogPath
            }
            return $false
        }
        $workerId = if ($null -ne $Cell.workerId) { [int]$Cell.workerId } else { -1 }
        if ($workerId -ge 0) {
            Set-AcWorkerWindowTitle -WindowHandle $hwnd -WorkerId $workerId -LogPath $LogPath | Out-Null
        }
        return (Set-AcWorkerWindowPosition -WindowHandle $hwnd -Cell $Cell -LogPath $LogPath)
    } catch {
        if ($LogPath) {
            Write-AiSessionLog ('window placement unavailable: ' + $_.Exception.Message) $LogPath
        }
        return $false
    }
}

function Get-AcVideoIniFpsLimit {
    param([string]$VideoIniPath)
    if (-not $VideoIniPath -or -not (Test-Path -LiteralPath $VideoIniPath)) { return $null }
    $capMs = Get-IniValue -Path $VideoIniPath -Section 'VIDEO' -Key 'FPS_CAP_MS'
    if ($null -eq $capMs -or $capMs -notmatch '^\s*[\d\.]+\s*$') { return $null }
    $ms = [double]$capMs
    if ($ms -le 0) { return $null }
    return [int][math]::Round(1000.0 / $ms)
}

function Get-AcWorkerWindowStatus {
    param(
        [int]$ProcessId,
        [string]$DocsVideoIni = '',
        [int]$MonitorIndex = 0
    )
    $status = [ordered]@{
        pid            = $ProcessId
        windowX        = $null
        windowY        = $null
        windowWidth    = $null
        windowHeight   = $null
        renderWidth    = $null
        renderHeight   = $null
        fullscreen     = $null
        fpsLimit       = $null
        monitorIndex   = $MonitorIndex
    }
    if ($DocsVideoIni -and (Test-Path -LiteralPath $DocsVideoIni)) {
        $status.renderWidth = Get-IniValue -Path $DocsVideoIni -Section 'VIDEO' -Key 'WIDTH'
        $status.renderHeight = Get-IniValue -Path $DocsVideoIni -Section 'VIDEO' -Key 'HEIGHT'
        $fs = Get-IniValue -Path $DocsVideoIni -Section 'VIDEO' -Key 'FULLSCREEN'
        $status.fullscreen = ($fs -eq '1')
        $idx = Get-IniValue -Path $DocsVideoIni -Section 'VIDEO' -Key 'INDEX'
        if ($null -ne $idx) { $status.monitorIndex = [int]$idx }
        $status.fpsLimit = Get-AcVideoIniFpsLimit -VideoIniPath $DocsVideoIni
    }
    if ($ProcessId -gt 0) {
        $hwnd = Get-AcProcessMainWindowByPid -ProcessId $ProcessId -TimeoutSec 2
        if ($hwnd -ne [IntPtr]::Zero) {
            $rect = Get-AcWindowRectInfo -WindowHandle $hwnd
            if ($rect) {
                $status.windowX = $rect.X
                $status.windowY = $rect.Y
                $status.windowWidth = $rect.Width
                $status.windowHeight = $rect.Height
            }
        }
    }
    return $status
}

function Test-AcBackgroundExecutionPolicy {
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [string]$Docs = ''
    )
    $candidateFiles = @(
        (Join-Path $RepoRoot 'ac_profiles\ai\cfg\gameplay.ini')
        (Join-Path $RepoRoot 'ac_profiles\ai\cfg\race.ini')
        (Join-Path $RepoRoot 'ac_profiles\ai\cfg\video.ini')
    )
    if ($Docs) {
        $candidateFiles += @(
            (Join-Path $Docs 'cfg\gameplay.ini')
            (Join-Path $Docs 'cfg\race.ini')
            (Join-Path $Docs 'cfg\video.ini')
        )
    }
    $patterns = @('PAUSE', 'BACKGROUND', 'UNFOCUS', 'MINIM', 'FOCUS')
    $hits = @()
    foreach ($file in ($candidateFiles | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $file)) { continue }
        foreach ($line in @(Get-Content -LiteralPath $file)) {
            $trim = $line.Trim()
            if ($trim -notmatch '^[A-Za-z0-9_]+=') { continue }
            foreach ($pat in $patterns) {
                if ($trim -match $pat) {
                    $hits += ($file + ': ' + $trim)
                    break
                }
            }
        }
    }
    $pauseSimKeys = @($hits | Where-Object { $_ -match 'PAUSE|BACKGROUND|UNFOCUS|MINIM' })
    if ($pauseSimKeys.Count -eq 0) {
        return @{
            Ok      = $false
            Warning = 'Aucune cle pause/arriere-plan documentee dans profils AI/Documents (gameplay.ini, race.ini, video.ini). Verifiez manuellement que AC ne se met pas en pause sans focus.'
            Hits    = @($hits)
        }
    }
    return @{ Ok = $true; Warning = $null; Hits = @($pauseSimKeys) }
}
