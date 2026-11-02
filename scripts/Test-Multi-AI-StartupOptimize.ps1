# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-Multi-AI-StartupOptimize.ps1') @args
exit $LASTEXITCODE
