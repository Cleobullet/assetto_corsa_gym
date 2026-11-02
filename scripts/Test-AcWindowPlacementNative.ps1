# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-AcWindowPlacementNative.ps1') @args
exit $LASTEXITCODE
