#Requires -Version 5.1
<#
.SYNOPSIS
    Test scripts Stop avec faux manifest/PID (sans processus reels).
#>
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
$LegacyScripts = Join-Path $RepoRoot 'legacy\scripts'
$ManifestPath = Join-Path $RepoRoot 'ac_workers\manifest.json'
$BackupManifest = $null
if (Test-Path -LiteralPath $ManifestPath) {
    $BackupManifest = Get-Content -LiteralPath $ManifestPath -Raw
}

$fake = @{
    schemaVersion = 1
    startedAtUtc  = (Get-Date).ToUniversalTime().ToString('o')
    aiInstallPath = 'C:\fake\ai'
    learnerPid    = 99999999
    workers       = @(
        @{
            workerId     = 0
            acPid        = 99999998
            collectorPid = 99999997
            ports        = @{ ego = 2345; opponents = 2346; mgmt = 2347 }
            vjoyDeviceId = 1
            logPath      = 'ac_workers/logs/worker_0_test.log'
            lockPath     = 'ac_workers/locks/worker_0.lock'
            state        = 'running'
        }
    )
} | ConvertTo-Json -Depth 6
Set-Content -LiteralPath $ManifestPath -Value $fake -Encoding UTF8

try {
    & (Join-Path $LegacyScripts 'Stop-AC-Workers.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Stop-AC-Workers exit non-zero' }
    & (Join-Path $ScriptsRoot 'Stop-Multi-AI-Session.ps1') -WorkerId 0
    if ($LASTEXITCODE -ne 0) { throw 'Stop-Multi-AI-Session exit non-zero' }
    $after = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    $remaining = @($after.workers | ForEach-Object { [int]$_.workerId })
    if ($remaining -contains 0) { throw 'worker 0 still present in manifest after targeted stop' }
    Write-Host 'Test-Stop-Scripts: OK'
} finally {
    if ($null -ne $BackupManifest) {
        Set-Content -LiteralPath $ManifestPath -Value $BackupManifest -Encoding UTF8
    } elseif (Test-Path -LiteralPath $ManifestPath) {
        Remove-Item -LiteralPath $ManifestPath -Force
    }
}
