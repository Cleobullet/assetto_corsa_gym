#Requires -Version 5.1
<#
.SYNOPSIS
    Global lifecycle control from the terminal: kill-all / status.
.EXAMPLE
    .\scripts\Ac-Ai-System.ps1 kill-all
    .\scripts\Ac-Ai-System.ps1 status
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('kill-all', 'status', 'health')]
    [string]$Command
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$VenvPython = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
$env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"

& $VenvPython -m ac_workers.system_control $Command
exit $LASTEXITCODE
