# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-WindowedSession-Static.ps1') @args
exit $LASTEXITCODE
