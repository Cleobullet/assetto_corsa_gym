# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-RedirectedProcess-Argv.ps1') @args
exit $LASTEXITCODE
