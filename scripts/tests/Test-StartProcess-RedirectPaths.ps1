#Requires -Version 5.1
<#
.SYNOPSIS
    Verifie qu aucun Start-Process n utilise le meme chemin stdout/stderr.
#>
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
$fail = @()

Get-ChildItem -LiteralPath (Join-Path $RepoRoot 'scripts') -Filter '*.ps1' -File | ForEach-Object {
    $content = Get-Content -LiteralPath $_.FullName -Raw
    if ($content -notmatch 'RedirectStandardOutput') { return }
    $lines = @(Get-Content -LiteralPath $_.FullName)
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $chunk = ($lines[$i..([Math]::Min($i + 4, $lines.Count - 1))] -join ' ')
        if ($chunk -notmatch 'RedirectStandardOutput') { continue }
        if ($chunk -notmatch 'RedirectStandardError') { continue }
        if ($chunk -match 'RedirectStandardOutput\s+\$(\w+).*RedirectStandardError\s+\$\1') {
            $fail += ($_.Name + ':' + ($i + 1) + ' meme variable stdout/stderr')
        }
        if ($chunk -match 'RedirectStandardError\s+\$(\w+).*RedirectStandardOutput\s+\$\1') {
            $fail += ($_.Name + ':' + ($i + 1) + ' meme variable stdout/stderr')
        }
    }
}

if ($fail.Count -gt 0) {
    $fail | ForEach-Object { Write-Host ('FAIL: ' + $_) }
    exit 1
}
Write-Host 'Test-StartProcess-RedirectPaths: OK'
exit 0
