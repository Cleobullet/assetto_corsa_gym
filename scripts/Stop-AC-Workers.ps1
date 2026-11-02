# Shim: moved to legacy\scripts after cleanup.
& (Join-Path (Split-Path -Parent $PSScriptRoot) 'legacy\scripts\Stop-AC-Workers.ps1') @args
exit $LASTEXITCODE
