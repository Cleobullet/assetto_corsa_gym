#Requires -Version 5.1
<#
.SYNOPSIS
    Tests hors ligne C4D: parse PS, Python offline, faux serveurs, DryRun 1/2/6.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
$LegacyScripts = Join-Path $RepoRoot 'legacy\scripts'
Set-Location $RepoRoot

$fail = $false
$psParse = @(
    @{ Name = 'Test-AI-Control-Link.ps1'; Dir = $ScriptDir },
    @{ Name = 'Emergency-Stop-AI.ps1'; Dir = $ScriptsRoot },
    @{ Name = 'Run-SAC-MultiWorker.ps1'; Dir = $ScriptsRoot },
    @{ Name = 'Start-Multi-AI-Session.ps1'; Dir = $ScriptsRoot },
    @{ Name = 'Ac-AiSessionCommon.ps1'; Dir = $ScriptsRoot },
    @{ Name = 'Init-Fresh-Policy.ps1'; Dir = $ScriptsRoot },
    @{ Name = 'Start-First-Training.ps1'; Dir = $LegacyScripts },
    @{ Name = 'Stop-First-Training.ps1'; Dir = $LegacyScripts },
    @{ Name = 'Start-C4D-ControlCenter.ps1'; Dir = $ScriptsRoot },
    @{ Name = 'Stop-C4D-ControlCenter.ps1'; Dir = $ScriptsRoot }
)
foreach ($s in $psParse) {
    $path = Join-Path $s.Dir $s.Name
    $null = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$null)
    Write-Host ('PS parse OK: ' + $s.Name)
}

$VenvPython = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
$env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
$prevEap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $VenvPython -m ac_workers.test_c4d_offline 2>&1 | ForEach-Object { Write-Host $_ }
$ErrorActionPreference = $prevEap
if ($LASTEXITCODE -ne 0) { $fail = $true }

& (Join-Path $ScriptDir 'Test-C4D-Collector-Import.ps1')
if ($LASTEXITCODE -ne 0) { $fail = $true }

. (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1')
$hcArgs = @('-m', 'ac_workers.transition_server_health', '--check', '--json', '--repo-root', $RepoRoot)
$hc = Invoke-PythonCapturedProcess -PythonExe $VenvPython -ArgumentList $hcArgs -WorkingDirectory $RepoRoot -TimeoutSec 30
if ($hc.Stderr -match 'Traceback') { $fail = $true; Write-Host 'FAIL health stderr contains Traceback' }
$hcLine = ($hc.Stdout -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
try {
    $null = $hcLine | ConvertFrom-Json
    Write-Host ('Health capture OK exit=' + $hc.ExitCode)
} catch {
    $fail = $true
    Write-Host ('FAIL health JSON: ' + $hcLine)
}

& (Join-Path $ScriptDir 'Test-AI-Control-Link.ps1') -DryRun
if ($LASTEXITCODE -ne 0) { $fail = $true }

& (Join-Path $ScriptsRoot 'Emergency-Stop-AI.ps1') -DryRun
if ($LASTEXITCODE -ne 0) { $fail = $true }

& (Join-Path $ScriptsRoot 'Run-SAC-MultiWorker.ps1') -Workers 1 -DryRun -SkipPrecheck
if ($LASTEXITCODE -ne 0) { $fail = $true }

& (Join-Path $ScriptsRoot 'Start-Multi-AI-Session.ps1') -Workers 1 -DryRun
if ($LASTEXITCODE -ne 0) { $fail = $true }

& (Join-Path $ScriptsRoot 'Start-Multi-AI-Session.ps1') -Workers 2 -DryRun
if ($LASTEXITCODE -ne 0) { $fail = $true }

& (Join-Path $ScriptsRoot 'Start-Multi-AI-Session.ps1') -Workers 6 -DryRun
if ($LASTEXITCODE -ne 0) { $fail = $true }

& (Join-Path $ScriptsRoot 'Start-Multi-AI-Session.ps1') -OnlyWorkerId 0 -Workers 6 -Windowed -DryRun
if ($LASTEXITCODE -ne 0) { $fail = $true }

& (Join-Path $ScriptsRoot 'Init-Fresh-Policy.ps1') -DryRun
if ($LASTEXITCODE -ne 0) { $fail = $true }

& (Join-Path $LegacyScripts 'Start-First-Training.ps1') -DryRun
if ($LASTEXITCODE -ne 0) { $fail = $true }

& (Join-Path $LegacyScripts 'Stop-First-Training.ps1') -DryRun
if ($LASTEXITCODE -ne 0) { $fail = $true }

& (Join-Path $ScriptDir 'Test-RedirectedProcess-Argv.ps1')
if ($LASTEXITCODE -ne 0) { $fail = $true }

& (Join-Path $LegacyScripts 'Test-Start-First-Training-Supervision.ps1')
if ($LASTEXITCODE -ne 0) { $fail = $true }

function Test-AiSessionManifestSchemaCases {
    param([string]$RepoRoot)
    Set-StrictMode -Version Latest
    $old = '{"transitionServerPid":8484,"learnerPid":null,"workers":[],"customField":"keep"}' | ConvertFrom-Json
    $m1 = Ensure-AiSessionManifestSchema -Manifest $old
    if ($null -ne $m1.transitionServerLauncherPid) { throw 'old launcher should be null' }
    if ($m1.customField -ne 'keep') { throw 'customField not preserved' }
    Write-Host 'OK manifest schema old launcher null'
    $null = Ensure-AiSessionManifestSchema -Manifest $m1
    Write-Host 'OK manifest schema double migrate'
    $null = Ensure-AiSessionManifestSchema -Manifest $null
    Write-Host 'OK manifest schema null input'
    $m4 = Ensure-AiSessionManifestSchema -Manifest ([pscustomobject]@{ transitionServerPid = 8484; transitionServerLauncherPid = 9448; workers = @(); sessionId = 't' })
    if ([int]$m4.transitionServerPid -ne 8484 -or [int]$m4.transitionServerLauncherPid -ne 9448) { throw 'listener/launcher distinct failed' }
    Write-Host 'OK manifest schema listener/launcher distinct'
    $backupPath = Join-Path $RepoRoot 'ac_workers\manifest.json'
    $backup = $null
    if (Test-Path -LiteralPath $backupPath) { $backup = Get-Content -LiteralPath $backupPath -Raw }
    try {
        Write-AiSessionManifest -RepoRoot $RepoRoot -Manifest ([ordered]@{
            schemaVersion = 1
            sessionId = '_offline_test'
            transitionServerPid = 8484
            transitionServerLauncherPid = 9448
            workers = @()
        })
        $loaded = Read-AiSessionManifest -RepoRoot $RepoRoot
        if ([int]$loaded.transitionServerPid -ne 8484 -or [int]$loaded.transitionServerLauncherPid -ne 9448) { throw 'atomic write pid mismatch' }
        Write-Host 'OK manifest atomic write'
    } finally {
        if ($null -ne $backup) {
            Set-Content -LiteralPath $backupPath -Value $backup -Encoding UTF8
        } elseif (Test-Path -LiteralPath $backupPath) {
            Remove-Item -LiteralPath $backupPath -Force
        }
    }
}
try {
    Test-AiSessionManifestSchemaCases -RepoRoot $RepoRoot
} catch {
    $fail = $true
    Write-Host ('FAIL manifest schema: ' + $_.Exception.Message)
}

if ($fail) {
    Write-Host 'Test-C4D-Offline: FAIL'
    exit 1
}
Write-Host 'Test-C4D-Offline: OK'
exit 0
