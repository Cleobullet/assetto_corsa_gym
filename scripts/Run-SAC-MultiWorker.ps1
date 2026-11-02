#Requires -Version 5.1



<#



.SYNOPSIS



    SAC central — precheck collector diag puis attache au transition server existant.



#>



[CmdletBinding()]



param(



    [ValidateRange(1, 6)]



    [int]$Workers = 1,



    [int]$MaxSteps = 0,



    [string]$WorkerIds = '',



    [switch]$Resume,



    [switch]$DryRun,



    [switch]$SkipPrecheck,



    [switch]$Continuous = $true



)







$ErrorActionPreference = 'Stop'



Set-StrictMode -Version Latest







$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path



$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path



Set-Location $RepoRoot







. (Join-Path $ScriptDir 'Ac-AiSessionCommon.ps1')

$VenvPython = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'



$env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"



$ManifestPath = Join-Path $RepoRoot 'ac_workers\manifest.json'







if ($Workers -eq 6) {



    & (Join-Path $ScriptDir 'tests\Test-Multi-AI-Preflight.ps1') -Workers 6



    if ($LASTEXITCODE -ne 0) { exit 1 }



}







if (-not $DryRun -and (Test-Path -LiteralPath $ManifestPath)) {

    $m = Read-AiSessionManifest -RepoRoot $RepoRoot

    if ($m.learnerPid) {



        $alive = $null -ne (Get-Process -Id $m.learnerPid -ErrorAction SilentlyContinue)



        if ($alive) {



            Write-Host ('learner already running pid=' + $m.learnerPid)



            exit 1



        }



    }



}







if (-not $SkipPrecheck -and -not $DryRun) {



    $verifyParams = @{

        PythonExe        = $VenvPython

        ArgumentList     = @('-m', 'ac_workers.transition_server_health', '--verify', '--repair-manifest', '--json')

        WorkingDirectory = $RepoRoot

        TimeoutSec       = 20

    }

    $vr = Invoke-PythonCapturedProcess @verifyParams



    $vrLine = ($vr.Stdout -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)



    if ($vr.ExitCode -ne 0 -or -not $vrLine) {



        Write-Host 'Run-SAC aborted: transition server verify failed'



        exit 1



    }



    try {



        $tsInfo = $vrLine | ConvertFrom-Json



    } catch {



        Write-Host 'Run-SAC aborted: transition server verify JSON invalid'



        exit 1



    }



    if (-not $tsInfo.ok) {



        Write-Host ('Run-SAC aborted: ' + $tsInfo.error)



        exit 1



    }



    if ($tsInfo.manifestPid -and $tsInfo.pid -and [int]$tsInfo.manifestPid -ne [int]$tsInfo.pid) {

        Write-Host ('transition server manifest repaired listener pid=' + $tsInfo.pid + ' was=' + $tsInfo.manifestPid)

    } else {

        Write-Host ('transition server verified pid=' + $tsInfo.pid)

    }



    if ($WorkerIds.Trim()) {

        & $VenvPython -m ac_workers.run_sac_precheck --worker-ids $WorkerIds --no-require-transitions

    } else {

        & $VenvPython -m ac_workers.run_sac_precheck --workers $Workers --no-require-transitions

    }



    if ($LASTEXITCODE -ne 0) {



        Write-Host 'Run-SAC aborted: collector precheck failed'



        exit 1



    }



    $statusPath = Join-Path $RepoRoot 'ac_workers\status\worker_0.json'



    if (-not (Test-Path -LiteralPath $statusPath)) {



        Write-Host 'Run-SAC aborted: worker_0.json absent'



        exit 1



    }



    $st = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json



    if ([int]$st.transition_count -le 0 -and [string]$st.phase -notin @('idle', 'running')) {



        Write-Host 'training refused: worker 0 has no valid transitions'



        Write-Host 'Run-SAC aborted: worker_0 transition_count=0'



        exit 1



    }



    if ([double]$st.telemetry_age_ms -gt 2000) {



        Write-Host ('Run-SAC aborted: worker_0 telemetry stale ms=' + $st.telemetry_age_ms)



        exit 1



    }



    if (-not $st.observation_complete) {



        Write-Host 'Run-SAC aborted: worker_0 observation incomplete'



        exit 1



    }



    $hbAge = $null

    if ($null -ne $st.heartbeat_monotonic) {

        $hbAge = [double]$st.heartbeat_age_ms

    } else {

        $hbAge = [double]$st.heartbeat_age_ms

    }

    if ($hbAge -gt 2000) {



        Write-Host ('Run-SAC aborted: worker_0 heartbeat stale ms=' + $hbAge)



        exit 1



    }



}







$argsPy = @('-m', 'ac_workers.multi_sac_trainer')



if ($MaxSteps -gt 0) { $argsPy += @('--max-steps', "$MaxSteps", '--no-continuous') }



if ($Continuous) { $argsPy += '--continuous' } else { $argsPy += '--no-continuous' }



if ($Resume) { $argsPy += '--resume' }



if ($DryRun) { $argsPy += '--dry-run' }







Write-Host ('Run-SAC-MultiWorker Workers=' + $Workers + ' MaxSteps=' + $MaxSteps + ' Resume=' + $Resume + ' Continuous=' + $Continuous + ' DryRun=' + $DryRun)

& $VenvPython @argsPy

exit $LASTEXITCODE


