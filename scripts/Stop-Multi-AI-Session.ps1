#Requires -Version 5.1

<#

.SYNOPSIS

    Arrete workers multi-AI sans tuer les autres si un seul est cible.

#>

[CmdletBinding()]

param(

    [int[]]$WorkerId,

    [switch]$StopLearner

)



$ErrorActionPreference = 'Stop'

Set-StrictMode -Version Latest



$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path

. (Join-Path $ScriptDir 'Ac-AiSessionCommon.ps1')



if (-not $WorkerId) {

    & (Join-Path $ScriptDir 'Stop-AC-Workers.ps1')

    exit 0

}



$manifestPath = Join-Path $RepoRoot 'ac_workers\manifest.json'

if (-not (Test-Path -LiteralPath $manifestPath)) {

    Write-Host 'Manifest absent.'

    exit 0

}

$manifest = Read-AiSessionManifest -RepoRoot $RepoRoot

foreach ($w in @($manifest.workers)) {

    if ($WorkerId -contains [int]$w.workerId) {

        foreach ($procField in @('collectorPid', 'collectorLauncherPid', 'acPid')) {

            $targetProcessId = $w.$procField

            if ($targetProcessId) {

                $alive = Get-Process -Id $targetProcessId -ErrorAction SilentlyContinue

                if ($alive) {

                    Stop-Process -Id $targetProcessId -Force -ErrorAction SilentlyContinue

                    Write-Host ('Stopped worker ' + $w.workerId + ' ' + $procField + '=' + $targetProcessId)

                } else {

                    Write-Host ('worker ' + $w.workerId + ' ' + $procField + '=' + $targetProcessId + ' deja arrete')

                }

            }

        }

    }

}

if ($StopLearner -and $manifest.learnerPid) {

    $learnerProcessId = [int]$manifest.learnerPid

    $learnerAlive = Get-Process -Id $learnerProcessId -ErrorAction SilentlyContinue

    if ($learnerAlive) {

        Stop-Process -Id $learnerProcessId -Force -ErrorAction SilentlyContinue

        Write-Host ('Stopped learner processId=' + $learnerProcessId)

    } else {

        Write-Host ('learner processId=' + $learnerProcessId + ' deja arrete')

    }

}

$VenvPython = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
$env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
$cleanupArgs = @('-m', 'ac_workers.session_cleanup', '--workers', ($WorkerId -join ','))
if ($StopLearner) { $cleanupArgs += '--learner' }
if (Test-Path -LiteralPath $VenvPython) {
    $cleanupOut = & $VenvPython @cleanupArgs 2>&1
    if ($cleanupOut) { Write-Host $cleanupOut }
} else {
    throw 'Python venv introuvable pour le nettoyage session.'
}

Write-Host ('Stop-Multi-AI-Session: cible WorkerId=' + ($WorkerId -join ','))

exit 0

