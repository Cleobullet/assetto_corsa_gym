# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-Ac-Ini-Integrity.ps1') @args
exit $LASTEXITCODE
