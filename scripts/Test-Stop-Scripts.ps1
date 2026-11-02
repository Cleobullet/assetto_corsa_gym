# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-Stop-Scripts.ps1') @args
exit $LASTEXITCODE
