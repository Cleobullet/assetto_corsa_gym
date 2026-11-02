# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-AI-Session-System.ps1') @args
exit $LASTEXITCODE
