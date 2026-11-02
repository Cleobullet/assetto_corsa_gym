#Requires -Version 5.1
<#
.SYNOPSIS
    Tests hors ligne modification conservative video.ini (copies temporaires).
#>
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
. (Join-Path $ScriptsRoot 'Get-AcLocalPaths.ps1')
. (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1')

$fail = @()
$src = Join-Path $RepoRoot 'ac_profiles\workers\worker_0\cfg\video.ini'
if (-not (Test-Path -LiteralPath $src)) {
    Write-Host ('FAIL: source absent ' + $src)
    exit 1
}

$tempDir = Join-Path $env:TEMP ('ac_video_ini_test_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $tempDir 'cfg') -Force | Out-Null
$tempIni = Join-Path $tempDir 'cfg\video.ini'
Copy-Item -LiteralPath $src -Destination $tempIni -Force

$steamVideo = Join-Path (Get-AcLocalPaths -RepoRoot $RepoRoot).SteamInstallPath 'cfg\video.ini'
$steamBefore = $null
if (Test-Path -LiteralPath $steamVideo) {
    $steamBefore = Get-Content -LiteralPath $steamVideo -Raw
}

$cell = @{
    workerId = 0; renderWidth = 640; renderHeight = 360
    screenX = 0; screenY = 0; screenWidth = 640; screenHeight = 360
}
Apply-AcWorkerVideoIniWindowed -Docs $tempDir -Cell $cell -MonitorIndex 0 -RenderPreset SixInstances | Out-Null

$fs = Get-IniValue -Path $tempIni -Section 'VIDEO' -Key 'FULLSCREEN'
$w = Get-IniValue -Path $tempIni -Section 'VIDEO' -Key 'WIDTH'
$h = Get-IniValue -Path $tempIni -Section 'VIDEO' -Key 'HEIGHT'
if ($fs -ne '0') { $fail += 'FULLSCREEN != 0' }
if ($w -ne '640') { $fail += ('WIDTH=' + $w) }
if ($h -ne '360') { $fail += ('HEIGHT=' + $h) }

$origFs = Get-IniValue -Path $tempIni -Section 'VIDEO' -Key '__ORIG_FULLSCREEN'
if ($null -eq $origFs) { $fail += '__ORIG_FULLSCREEN absent' }

$post = Get-IniValue -Path $tempIni -Section 'POST_PROCESS' -Key 'ENABLED'
if ($post -ne '0') { $fail += 'POST_PROCESS.ENABLED non reduit' }

$lineCountBefore = (Get-Content -LiteralPath $src).Count
$lineCountAfter = (Get-Content -LiteralPath $tempIni).Count
if ($lineCountAfter -lt $lineCountBefore) {
    $fail += 'video.ini: lignes supprimees (reconstruction?)'
}

$audit = Get-AcVideoIniKeysAudit -Path $tempIni
$required = @('Fullscreen', 'Width', 'Height', 'FpsCapMs')
foreach ($k in $required) {
    if (-not $audit.Contains($k)) { $fail += ('cle audit absente: ' + $k) }
}

if ($steamBefore -and (Test-Path -LiteralPath $steamVideo)) {
    $steamAfter = Get-Content -LiteralPath $steamVideo -Raw
    if ($steamAfter -ne $steamBefore) {
        $fail += 'Steam video.ini modifie'
    }
}

Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue

if ($fail.Count -gt 0) {
    $fail | ForEach-Object { Write-Host ('FAIL: ' + $_) }
    exit 1
}
Write-Host 'Test-VideoIniWindowed: OK'
Write-Host ('Cles identifiees: ' + (($audit.Keys | ForEach-Object { $audit[$_].Section + '.' + $audit[$_].Key }) -join ', '))
exit 0
