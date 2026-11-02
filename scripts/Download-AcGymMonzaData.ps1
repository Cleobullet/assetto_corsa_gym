#Requires -Version 5.1
<#
.SYNOPSIS
    Telecharge monza_0.1m.pkl depuis le dataset Hugging Face (Phase C2A).

.DESCRIPTION
    Utilise .venv-acgym. Ne lit pas le contenu du pickle.
    Reutilise le fichier s'il existe deja (taille > 0).
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Assert-LastExitCode {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        throw "Echec: $Step (exit code $LASTEXITCODE)"
    }
}

$DestDir = Join-Path $RepoRoot "assetto_corsa_gym\AssettoCorsaConfigs\tracks"
$DestFile = Join-Path $DestDir "monza_0.1m.pkl"

if (Test-Path -LiteralPath $DestFile) {
    $existing = Get-Item -LiteralPath $DestFile
    if ($existing.Length -gt 0) {
        Write-Host "Deja present: $($existing.FullName) ($($existing.Length) bytes) - rien a faire."
        exit 0
    }
}

$VenvPython = Join-Path $RepoRoot ".venv-acgym\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Venv introuvable: $VenvPython - lancez Setup-AcGymEnvironment.ps1 d'abord."
}

if (-not (Test-Path -LiteralPath $DestDir)) {
    New-Item -ItemType Directory -Path $DestDir -Force | Out-Null
}

Write-Host "Installation huggingface_hub dans .venv-acgym (si necessaire)..."
& $VenvPython -m pip install "huggingface_hub>=0.20.0"
Assert-LastExitCode -Step "pip install huggingface_hub"

$PyScript = @'
import shutil
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download

repo_root = Path(sys.argv[1])
dest = repo_root / "assetto_corsa_gym" / "AssettoCorsaConfigs" / "tracks" / "monza_0.1m.pkl"
dest.parent.mkdir(parents=True, exist_ok=True)

cache_path = hf_hub_download(
    repo_id="dasgringuen/assettoCorsaGym",
    repo_type="dataset",
    filename="AssettoCorsaConfigs/tracks/monza_0.1m.pkl",
)
shutil.copy2(cache_path, dest)

if not dest.is_file() or dest.stat().st_size <= 0:
    raise SystemExit(f"Fichier invalide apres copie: {dest}")

print(f"OK {dest} ({dest.stat().st_size} bytes)")
'@

Write-Host "Telechargement AssettoCorsaConfigs/tracks/monza_0.1m.pkl ..."
$PyScript | & $VenvPython - $RepoRoot
Assert-LastExitCode -Step "hf_hub_download monza_0.1m.pkl"

if (-not (Test-Path -LiteralPath $DestFile)) {
    throw "Fichier absent: $DestFile"
}

$info = Get-Item -LiteralPath $DestFile
if ($info.Length -le 0) {
    throw "Fichier vide: $DestFile"
}
Write-Host "Fichier final: $($info.FullName) ($($info.Length) bytes)"

Write-Host "Restauration typing_extensions==4.3.0 ..."
& $VenvPython -m pip install "typing_extensions==4.3.0"
Assert-LastExitCode -Step "pip install typing_extensions==4.3.0"

Write-Host "Verification imports torch, gym, numpy ..."
& $VenvPython -c "import torch; import gym; import numpy; print('torch', torch.__version__); print('gym', gym.__version__); print('numpy', numpy.__version__)"
Assert-LastExitCode -Step "import torch gym numpy"

Write-Host "OK Download-AcGymMonzaData termine."
