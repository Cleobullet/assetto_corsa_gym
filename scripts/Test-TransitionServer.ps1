# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-TransitionServer.ps1') @args
exit $LASTEXITCODE
