# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-AI-Camera-Inputs.ps1') @args
exit $LASTEXITCODE
