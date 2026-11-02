#Requires -Version 5.1
<#
.SYNOPSIS
    Verifie les imports collector avant lancement AC (fail-fast).
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
. (Join-Path $ScriptsRoot 'Get-AcLocalPaths.ps1')
$VenvPython = Resolve-AcGymPython -RepoRoot $RepoRoot
$env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"

$modules = @(
    'ac_workers.worker_collector',
    'ac_workers.worker_handshake',
    'ac_workers.collector_diag',
    'ac_workers.collector_safety'
)

$py = @"
import importlib, sys
mods = sys.argv[1:]
fail = False
for m in mods:
    try:
        importlib.import_module(m)
        print('IMPORT OK', m)
    except Exception as exc:
        fail = True
        print('IMPORT FAIL', m, type(exc).__name__ + ':', exc)
sys.exit(1 if fail else 0)
"@

$argsPy = @('-c', $py) + $modules
& $VenvPython @argsPy
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Test-C4D-Collector-Import: FAIL'
    exit 1
}
Write-Host 'Test-C4D-Collector-Import: OK'
exit 0
