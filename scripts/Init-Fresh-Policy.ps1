<#
.SYNOPSIS
    Crée une policy SAC vierge et compatible avec l'environnement C4D actuel (state_dim=125).
.DESCRIPTION
    Inspecte les dimensions de l'environnement, initialise les poids aléatoires,
    publie un premier checkpoint avec métadonnées.
    Ne lance ni AC ni Content Manager.
.PARAMETER StateDim
    Dimension de l'espace d'observation (default: 125).
.PARAMETER WorkDir
    Répertoire de travail de la policy (default: outputs/multi_sac_cpu_v3).
.PARAMETER ConfigPath
    Chemin du fichier de config SAC (default: config_first_sac_cpu_v3.yml).
.PARAMETER DryRun
    Afficher les paramètres sans créer de fichiers.
#>
[CmdletBinding()]
param(
    [int]$StateDim = 125,
    [int]$ActionDim = 3,
    [string]$WorkDir = "",
    [string]$ConfigPath = "",
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path

if (-not $WorkDir) {
    $WorkDir = Join-Path $RepoRoot "outputs\multi_sac_cpu_v3"
}
if (-not $ConfigPath) {
    $ConfigPath = Join-Path $RepoRoot "config_first_sac_cpu_v3.yml"
}

$VenvPython = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'
$env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"

if ($DryRun) {
    Write-Host "DryRun: Init-Fresh-Policy state_dim=$StateDim action_dim=$ActionDim"
    Write-Host "  config=$ConfigPath"
    Write-Host "  work_dir=$WorkDir"
    Write-Host "Init-Fresh-Policy DryRun: OK"
    exit 0
}

& $VenvPython -m ac_workers.init_fresh_policy `
    --state-dim $StateDim `
    --action-dim $ActionDim `
    --config "$ConfigPath" `
    --work-dir "$WorkDir"

if ($LASTEXITCODE -ne 0) {
    Write-Host "Init-Fresh-Policy: FAIL"
    exit 1
}

Write-Host "Init-Fresh-Policy: OK"
