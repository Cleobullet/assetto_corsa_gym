# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-Presentation-Camera-Config.ps1') @args
exit $LASTEXITCODE
