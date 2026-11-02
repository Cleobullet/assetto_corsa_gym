#Requires -Version 5.1
<#
.SYNOPSIS
    Test liaison IA reelle worker 0 (C4D). -ReadOnly sans commande vJoy.
#>
[CmdletBinding()]
param(
    [int]$WorkerId = 0,
    [switch]$ReadOnly,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
$VenvPython = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
$env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"

$argsPy = @('-m', 'ac_workers.control_link_test', '--worker-id', "$WorkerId")
if ($ReadOnly) { $argsPy += '--read-only' }
if ($DryRun) { $argsPy += '--dry-run' }

& $VenvPython @argsPy
exit $LASTEXITCODE
