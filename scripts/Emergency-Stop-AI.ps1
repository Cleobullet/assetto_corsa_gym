#Requires -Version 5.1
<#
.SYNOPSIS
    Arret d urgence IA: neutre vJoy, stop collectors/learner, et ferme les fenetres AC workers (-CloseGame).
#>
[CmdletBinding()]
param(
    [switch]$CloseGame,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$VenvPython = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
$env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"

$argsPy = @('-m', 'ac_workers.emergency_stop')
# Toujours fermer les fenêtres acs.exe des workers (arrêt d'urgence = coupure totale).
# -CloseGame reste accepté pour compatibilité avec les anciens appels.
$argsPy += '--close-game'
if ($DryRun) { $argsPy += '--dry-run' }

& $VenvPython @argsPy
exit $LASTEXITCODE
