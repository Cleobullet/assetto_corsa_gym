# Shim: moved to scripts\tests after cleanup.
& (Join-Path $PSScriptRoot 'tests\Test-StartProcess-RedirectPaths.ps1') @args
exit $LASTEXITCODE
