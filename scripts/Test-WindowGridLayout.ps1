# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-WindowGridLayout.ps1') @args
exit $LASTEXITCODE
