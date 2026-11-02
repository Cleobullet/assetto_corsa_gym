# Shim: preflight moved to scripts\tests after project cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-Multi-AI-Preflight.ps1') @args
exit $LASTEXITCODE
