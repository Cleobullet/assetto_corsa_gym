# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-WindowPlacementNonFatal.ps1') @args
exit $LASTEXITCODE
