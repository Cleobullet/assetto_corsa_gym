#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
Set-Location $RepoRoot
. (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1')

$VenvPython = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
$ConfigPath = Join-Path $RepoRoot 'config_first_sac_cpu_v3.yml'
$WorkDir = Join-Path $RepoRoot 'outputs\multi_sac_cpu_v3'

$captureArgs = @(
    '-m', 'ac_workers.test_argv_capture',
    '--config', $ConfigPath,
    '--work-dir', $WorkDir,
    '--max-steps', '42',
    '--session-id', 'test_session',
    '--argv-out', 'IGNORED',
    '--exit-code', '0'
)

$captured = Invoke-PythonCapturedProcess -PythonExe $VenvPython -ArgumentList $captureArgs -WorkingDirectory $RepoRoot -TimeoutSec 60
if ($captured.ExitCode -ne 0) {
    throw ('Test-RedirectedProcess-Argv: capture exit code {0}' -f $captured.ExitCode)
}
$received = @((ConvertFrom-Json -InputObject $captured.Stdout.Trim()))
if ([string]$received[0] -notmatch 'test_argv_capture\.py$') {
    throw ('Test-RedirectedProcess-Argv: unexpected argv0: {0}' -f $received[0])
}
$configIdx = [array]::IndexOf($received, '--config')
$workIdx = [array]::IndexOf($received, '--work-dir')
$stepsIdx = [array]::IndexOf($received, '--max-steps')
$sessionIdx = [array]::IndexOf($received, '--session-id')
if ($configIdx -lt 0 -or [string]$received[$configIdx + 1] -ne $ConfigPath) {
    throw ('Test-RedirectedProcess-Argv: config path not preserved: {0}' -f $received[$configIdx + 1])
}
if ($workIdx -lt 0 -or [string]$received[$workIdx + 1] -ne $WorkDir) {
    throw ('Test-RedirectedProcess-Argv: work-dir path not preserved: {0}' -f $received[$workIdx + 1])
}
if ($stepsIdx -lt 0 -or [string]$received[$stepsIdx + 1] -ne '42') {
    throw 'Test-RedirectedProcess-Argv: max-steps mismatch'
}
if ($sessionIdx -lt 0 -or [string]$received[$sessionIdx + 1] -ne 'test_session') {
    throw 'Test-RedirectedProcess-Argv: session-id mismatch'
}

$uid = [guid]::NewGuid().ToString('N')
$Stdout = Join-Path $env:TEMP ("argv_stdout_{0}.log" -f $uid)
$Stderr = Join-Path $env:TEMP ("argv_stderr_{0}.log" -f $uid)
$exitArgs = @('-c', 'import sys; sys.exit(7)')
$proc = Start-AiRedirectedProcess `
    -FilePath $VenvPython `
    -ArgumentList $exitArgs `
    -WorkingDirectory $RepoRoot `
    -StdoutLogPath $Stdout `
    -StderrLogPath $Stderr `
    -Hidden
if (-not $proc.WaitForExit(30000)) {
    throw 'Test-RedirectedProcess-Argv: redirected process timeout'
}
$proc.Refresh()
if ($proc.ExitCode -ne 7) {
    throw ('Test-RedirectedProcess-Argv: redirected exit code {0} expected 7' -f $proc.ExitCode)
}

Write-Host 'Test-RedirectedProcess-Argv: OK'
exit 0
