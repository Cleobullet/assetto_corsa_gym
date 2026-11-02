# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-AI-Control-Link.ps1') @args
exit $LASTEXITCODE
