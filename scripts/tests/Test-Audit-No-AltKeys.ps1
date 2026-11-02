#Requires -Version 5.1
<#
.SYNOPSIS
    Audit statique: aucune simulation Alt pour le focus; Release-StuckModifierKeys KEYUP only.
#>
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
$common = Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1'
$text = Get-Content -LiteralPath $common -Raw

$fail = @()
if ($text -match 'SendKeys|keybd_event|AppActivate|%[\{\(]') {
    $fail += 'pattern SendKeys/keybd/AppActivate/% Alt trouve dans scripts'
}
if ($text -notmatch 'FocusWindowNoKeyboard') {
    $fail += 'FocusWindowNoKeyboard absent'
}
if ($text -notmatch 'AttachThreadInput') {
    $fail += 'AttachThreadInput absent'
}
if ($text -match 'SendKey\(\[AcAiSendInput\]::VK_LMENU,\s*\$false\)|SendKey\(\[AcAiSendInput\]::VK_RMENU,\s*\$false\)') {
    $fail += 'KEYDOWN Alt detecte'
}
if ($text -notmatch 'ReleaseModifier') {
    $fail += 'ReleaseModifier absent'
}

$pluginCfg = Join-Path $RepoRoot 'assetto_corsa_gym\AssettoCorsaPlugin\plugins\sensors_par\config.py'
$cfgText = Get-Content -LiteralPath $pluginCfg -Raw
if ($cfgText -notmatch 'enable_debug_hotkeys = False') {
    $fail += 'plugin config enable_debug_hotkeys != False'
}

$sensors = Join-Path $RepoRoot 'assetto_corsa_gym\AssettoCorsaPlugin\plugins\sensors_par\sensors_par.py'
$sensText = Get-Content -LiteralPath $sensors -Raw
if ($sensText -notmatch 'enable_debug_hotkeys and ac\.ext_isAltPressed') {
    $fail += 'sensors_par Alt block not gated on enable_debug_hotkeys'
}

if ($fail.Count -gt 0) {
    $fail | ForEach-Object { Write-Host ('FAIL: ' + $_) }
    exit 1
}
Write-Host 'Test-Audit-No-AltKeys: OK (focus sans Alt, release KEYUP only, plugin hotkeys off)'
exit 0
