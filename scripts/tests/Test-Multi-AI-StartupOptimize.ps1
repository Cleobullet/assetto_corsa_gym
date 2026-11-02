#Requires -Version 5.1
<#
.SYNOPSIS
    Tests statiques optimisation demarrage multi-worker (sans AC/CM/SAC).
#>
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
$fail = @()

$start = Get-Content -LiteralPath (Join-Path $ScriptsRoot 'Start-Multi-AI-Session.ps1') -Raw
$common = Get-Content -LiteralPath (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1') -Raw
$install = Get-Content -LiteralPath (Join-Path $ScriptsRoot 'Install-AcGymPlugin-AI.ps1') -Raw

if ($start -match 'StartupBoost') { $fail += 'StartupBoost present' }
if ($start -notmatch 'SkipIfUpToDate') { $fail += 'plugin SkipIfUpToDate absent du lanceur' }
if ($start -notmatch 'Wait-AcWorkerLauncherReady') { $fail += 'Wait-AcWorkerLauncherReady absent' }
if ($start -notmatch 'Write-AcWorkerLaunchTimings') { $fail += 'timings absents' }
if ($start -match 'Start-Sleep -Seconds (5|10)') { $fail += 'Start-Sleep long fixe encore present' }
if ($start -notmatch 'launcherReadyWorkers') { $fail += 'collectors non decouples du launcher' }
if ($common -notmatch 'PollMs = 200') { $fail += 'polling 200ms absent de Wait-ManagementPort' }
if ($install -notmatch 'Test-TreeHashesMatch') { $fail += 'hash plugin absent' }

$errs = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $ScriptsRoot 'Start-Multi-AI-Session.ps1'), [ref]$null, [ref]$errs)
if ($errs) { $fail += ('parse Start-Multi: ' + ($errs -join '; ')) }

& (Join-Path $ScriptDir 'Test-Stop-Scripts.ps1')
if ($LASTEXITCODE -ne 0) { $fail += 'Test-Stop-Scripts FAIL' }

& (Join-Path $ScriptsRoot 'Start-Multi-AI-Session.ps1') -DryRun -Workers 2 -Windowed -WindowLayout Auto `
    -RenderPreset SixInstances -PresentationMode -PresentationCamera ThirdPerson -PresentationChaseIndex 0
if ($LASTEXITCODE -ne 0) { $fail += 'DryRun 2 workers FAIL' }

if ($fail.Count -gt 0) {
    $fail | ForEach-Object { Write-Host ('FAIL: ' + $_) }
    exit 1
}
Write-Host 'Test-Multi-AI-StartupOptimize: OK'
exit 0
