#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir '..\Ac-AiSessionCommon.ps1')

$merged = @(Merge-ManifestWorkerEntries -ExistingWorkers @(
    [pscustomobject]@{ workerId = 0; collectorPid = 11 }
    [pscustomobject]@{ workerId = 1; collectorPid = 22 }
    [pscustomobject]@{ workerId = 5; collectorPid = 99 }
) -NewWorkers @(
    [ordered]@{ workerId = 5; collectorPid = 55 }
))
$ids = @($merged | ForEach-Object { [int]$_.workerId })
if (@($ids | Sort-Object) -join ',' -ne '0,1,5') { throw ('ids inattendus: ' + ($ids -join ',')) }
$worker5 = @($merged | Where-Object { [int]$_.workerId -eq 5 })[0]
if ([int]$worker5.collectorPid -ne 55) { throw 'le worker 5 relance doit remplacer l ancien' }
Write-Host 'Test-ManifestMerge: OK'
