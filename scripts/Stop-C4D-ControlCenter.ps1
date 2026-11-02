#Requires -Version 5.1
<#
.SYNOPSIS
    Arrete uniquement le C4D Control Center (serveur, dashboard, console) pour ce depot.
    Ne touche pas AC, CM, transition server, collectors ni learner.
#>
[CmdletBinding()]
param(
    [int]$Port = 24700
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
Set-Location $RepoRoot

function Resolve-C4DPython {
    $dir = $RepoRoot
    while ($dir) {
        $candidate = Join-Path $dir '.venv-acgym\Scripts\python.exe'
        if (Test-Path -LiteralPath $candidate) { return $candidate }
        $parent = Split-Path $dir -Parent
        if (-not $parent -or $parent -eq $dir) { break }
        $dir = $parent
    }
    throw 'Python venv introuvable: .venv-acgym\Scripts\python.exe'
}
$VenvPython = Resolve-C4DPython

$env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
$reportRaw = & $VenvPython -m ac_workers.c4d_server_lock stop --repo-root $RepoRoot --port $Port
if ($LASTEXITCODE -ne 0) { throw $reportRaw }
$report = $reportRaw | ConvertFrom-Json
$count = @($report.stopped_pids).Count
Write-Host ('Stop-C4D-ControlCenter: stopped ' + $count + ' process(es) for repo')
exit 0
