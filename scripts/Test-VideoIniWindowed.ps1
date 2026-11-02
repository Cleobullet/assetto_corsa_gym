# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-VideoIniWindowed.ps1') @args
exit $LASTEXITCODE
