#Requires -Version 5.1
<#
.SYNOPSIS
    Cree un environnement Python isole pour Assetto Corsa Gym (Phase C2A, smoke test CPU).

.DESCRIPTION
    - Python 3.9.x requis (aligne README / gym 0.21)
    - PyTorch CPU uniquement (pas de CUDA / cudatoolkit)
    - N'installe pas jupyter, wandb, pybullet, datasets Hugging Face

.PARAMETER PythonExe
    Chemin explicite vers python.exe 3.9. Sinon detection automatique.

.PARAMETER VenvPath
    Dossier venv relatif a la racine du depot (defaut: .venv-acgym)

.EXAMPLE
    .\scripts\Setup-AcGymEnvironment.ps1
#>
[CmdletBinding()]
param(
    [string]$PythonExe = "",
    [string]$VenvPath = ".venv-acgym"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Assert-LastExitCode {
    param(
        [string]$Step
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Echec: $Step (exit code $LASTEXITCODE)"
    }
}

function Invoke-VenvPip {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$PipArgs,
        [string]$Label = ""
    )
    if (-not $Label) {
        $Label = "pip " + ($PipArgs -join " ")
    }
    & $script:VenvPython -m pip @PipArgs
    Assert-LastExitCode -Step $Label
}

function Invoke-VenvPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Code,
        [string]$Label = "python -c"
    )
    & $script:VenvPython -c $Code
    Assert-LastExitCode -Step $Label
}

function Find-Python39 {
    param([string]$Explicit)
    if ($Explicit -and (Test-Path -LiteralPath $Explicit)) {
        return (Resolve-Path -LiteralPath $Explicit).Path
    }

    $candidates = @(
        { & py -3.9 -c "import sys; print(sys.executable)" 2>$null },
        { Join-Path $env:LOCALAPPDATA "Programs\Python\Python39\python.exe" },
        { Join-Path $env:USERPROFILE "anaconda3\envs\p309\python.exe" },
        { Join-Path $env:USERPROFILE "miniconda3\envs\p309\python.exe" },
        { Join-Path $env:USERPROFILE "anaconda3\envs\acgym39\python.exe" }
    )

    foreach ($candidate in $candidates) {
        try {
            $path = & $candidate
            if ($path -and (Test-Path -LiteralPath $path)) {
                $version = & $path -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
                Assert-LastExitCode -Step "python version check"
                if ($version -eq "3.9") {
                    return (Resolve-Path -LiteralPath $path).Path
                }
            }
        } catch {
            continue
        }
    }

    throw @"
Python 3.9 introuvable.
Installez Python 3.9.13 (README: conda create -n p309 python=3.9.13)
ou relancez avec -PythonExe 'C:\chemin\vers\python.exe'
"@
}

function Test-TorchCpuReady {
    param(
        [string]$VenvPython
    )
    try {
        $version = & $VenvPython -c "import torch; print(torch.__version__)"
        if ($LASTEXITCODE -ne 0) {
            return $false
        }
        return ($version -eq "1.12.1+cpu")
    } catch {
        return $false
    }
}

function Test-ImportGym {
    param(
        [string]$VenvPython
    )
    & $VenvPython -c "import gym"
    return ($LASTEXITCODE -eq 0)
}

$basePython = Find-Python39 -Explicit $PythonExe
Write-Host "Python de base : $basePython"

$venvFullPath = Join-Path $RepoRoot $VenvPath
$script:VenvPython = Join-Path $venvFullPath "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $script:VenvPython)) {
    Write-Host "Creation du venv : $venvFullPath"
    & $basePython -m venv $venvFullPath
    Assert-LastExitCode -Step "python -m venv"
} else {
    Write-Host "Venv existant : $venvFullPath"
}

Write-Host "Installation pip / setuptools / wheel (pins legacy gym)..."
Invoke-VenvPip -PipArgs @("install", "--upgrade", "pip==24.0", "setuptools==65.5.0", "wheel==0.38.4") `
    -Label "pip install pip setuptools wheel"

if (Test-TorchCpuReady -VenvPython $script:VenvPython) {
    Write-Host "PyTorch 1.12.1+cpu deja installe - skip."
} else {
    Write-Host "Installation PyTorch 1.12.1 (CPU, sans CUDA)..."
    Invoke-VenvPip -PipArgs @(
        "install", "torch==1.12.1", "--index-url", "https://download.pytorch.org/whl/cpu"
    ) -Label "pip install torch cpu"
}

Write-Host "Installation gym==0.21.0 (--no-build-isolation)..."
Invoke-VenvPip -PipArgs @("install", "gym==0.21.0", "--no-build-isolation") `
    -Label "pip install gym --no-build-isolation"

if (-not (Test-ImportGym -VenvPython $script:VenvPython)) {
    throw "import gym a echoue apres installation gym==0.21.0"
}

Write-Host "Installation autres dependances smoke test..."
$requirementsPath = Join-Path $RepoRoot "requirements-acgym-smoke.txt"
Invoke-VenvPip -PipArgs @("install", "-r", $requirementsPath) -Label "pip install -r requirements-acgym-smoke.txt"

Write-Host "Verification torch CPU..."
$torchInfo = & $script:VenvPython -c "import torch; print(f'torch={torch.__version__} cuda={torch.cuda.is_available()}')"
Assert-LastExitCode -Step "import torch"
Write-Host $torchInfo
if ($torchInfo -match "cuda=True") {
    throw "CUDA detecte - ce setup exige PyTorch CPU uniquement (AMD RX 9070 XT)."
}

Write-Host "Verification imports finaux..."
$importOutput = & $script:VenvPython -c @"
import torch
import gym
import numpy
import pandas
import omegaconf
import box
print('torch', torch.__version__)
print('gym', gym.__version__)
print('numpy', numpy.__version__)
print('pandas', pandas.__version__)
print('omegaconf', omegaconf.__version__)
print('box ok')
"@
Assert-LastExitCode -Step "import smoke test (torch, gym, numpy, pandas, omegaconf, box)"
$importOutput | ForEach-Object { Write-Host $_ }

Write-Host ""
Write-Host "OK environnement pret : $script:VenvPython"
Write-Host "Prochaine etape (session AI + sensors_par actifs, track/car = config.yml) :"
Write-Host "  $script:VenvPython scripts\tests\Smoke-Test-AcGym.py"
