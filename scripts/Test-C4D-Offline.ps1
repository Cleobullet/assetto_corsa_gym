# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-C4D-Offline.ps1') @args
exit $LASTEXITCODE
