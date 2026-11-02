# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-Ac-Plugin-Import.ps1') @args
exit $LASTEXITCODE
