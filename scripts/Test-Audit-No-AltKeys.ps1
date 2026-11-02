# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-Audit-No-AltKeys.ps1') @args
exit $LASTEXITCODE
