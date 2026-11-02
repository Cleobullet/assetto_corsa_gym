#Requires -Version 5.1
<#
.SYNOPSIS
    Tests d'import dual-context sensors_par hors ligne.
#>
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$PluginSrc = Join-Path $RepoRoot 'assetto_corsa_gym\AssettoCorsaPlugin\plugins\sensors_par'
$VenvPython = Join-Path $RepoRoot '.venv-acgym\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host 'Test-Ac-Plugin-Import: SKIP (venv absent)'
    exit 0
}

$pyTests = @'
import ast
import os
import sys

repo = sys.argv[1]
plugin = sys.argv[2]
failures = []

for name in os.listdir(plugin):
    if name.endswith(".py"):
        path = os.path.join(plugin, name)
        with open(path, "r") as f:
            src = f.read()
        try:
            ast.parse(src, path)
        except SyntaxError as e:
            failures.append("ast fail " + name + ": " + str(e))

sys.path.insert(0, os.path.join(repo, "assetto_corsa_gym"))
import AssettoCorsaPlugin.plugins.sensors_par.car_control as cc_pkg
if cc_pkg.vJoy is None:
    failures.append("package import vJoy None")

sys.path.insert(0, plugin)
if "car_control" in sys.modules:
    del sys.modules["car_control"]
if "vjoy" in sys.modules:
    del sys.modules["vjoy"]
import car_control as cc_top
if cc_top.vJoy is None:
    failures.append("top-level import vJoy None")

if failures:
    for f in failures:
        print("FAIL " + f)
    sys.exit(1)
print("OK")
'@

$tmpPy = Join-Path ([System.IO.Path]::GetTempPath()) ('test_plugin_import_' + [guid]::NewGuid().ToString('N') + '.py')
Set-Content -LiteralPath $tmpPy -Value $pyTests -Encoding UTF8
try {
    $global:LASTEXITCODE = 0
    & $VenvPython $tmpPy $RepoRoot $PluginSrc
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Test-Ac-Plugin-Import: FAIL (imports)'
        exit 1
    }
    Write-Host 'Test-Ac-Plugin-Import: imports OK'
} finally {
    Remove-Item -LiteralPath $tmpPy -Force -ErrorAction SilentlyContinue
}

. (Join-Path (Split-Path -Parent $PSScriptRoot) 'Get-AcLocalPaths.ps1')
$aiRoot = (Get-AcLocalPaths -RepoRoot $RepoRoot).AiInstallPath
& (Join-Path (Split-Path -Parent $PSScriptRoot) 'Install-AcGymPlugin-AI.ps1') -RepoRoot $RepoRoot -SkipBackup
if (-not $?) {
    Write-Host 'Install-AcGymPlugin-AI.ps1: FAIL'
    exit 1
}

$installed = Join-Path $aiRoot 'apps\python\sensors_par\car_control.py'
$content = Get-Content -LiteralPath $installed -Raw
if ($content -notmatch '_has_package' -or $content -notmatch 'SystemError') {
    Write-Host 'Correction import absente dans copie AI: FAIL'
    exit 1
}
Write-Host 'Test-Ac-Plugin-Import: OK'
exit 0
