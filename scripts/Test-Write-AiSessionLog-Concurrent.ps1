# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-Write-AiSessionLog-Concurrent.ps1') @args
exit $LASTEXITCODE
