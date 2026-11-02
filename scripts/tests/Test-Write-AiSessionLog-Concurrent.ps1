#Requires -Version 5.1
<#
.SYNOPSIS
    Test concurrent Write-AiSessionLog (simule orchestrateur + workers).
#>
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
. (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1')

$LogDir = Join-Path (Resolve-Path (Join-Path $ScriptDir '..\..')).Path 'ac_workers\logs\_test_concurrent'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$SharedLog = Join-Path $LogDir ('concurrent_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.log')

$jobs = @()
1..6 | ForEach-Object {
    $workerNum = $_
    $jobs += Start-Job -ScriptBlock {
        param($LogPath, $WorkerNum, $CommonPath)
        . $CommonPath
        1..40 | ForEach-Object {
            Write-AiSessionLog ("worker $WorkerNum line $_") $LogPath
        }
    } -ArgumentList $SharedLog, $workerNum, (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1')
}

Wait-Job -Job $jobs | Out-Null
$jobs | Remove-Job -Force

$lines = @(Get-Content -LiteralPath $SharedLog -ErrorAction SilentlyContinue)
if ($lines.Count -lt 200) {
    Write-Host ('FAIL: expected ~240 lines, got ' + $lines.Count)
    exit 1
}
Write-Host ('Test-Write-AiSessionLog-Concurrent: OK lines=' + $lines.Count)
