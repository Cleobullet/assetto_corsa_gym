#Requires -Version 5.1
# Raccourcis du centre de contrôle Windows ; le lanceur reconstruit les sources modifiées.
param([switch]$ProjectOnly)
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$launcher = Join-Path $PSScriptRoot 'Open-AI-App.ps1'
$icon = Join-Path $repoRoot 'desktop\AcAiControl\Assets\app.ico'
$links = @((Join-Path $repoRoot 'Application AI.lnk'), (Join-Path $repoRoot 'Assetto Corsa AI.lnk'))
if (-not $ProjectOnly) {
    $links += Join-Path ([Environment]::GetFolderPath('Desktop')) 'Assetto Corsa AI.lnk'
}
$shell = New-Object -ComObject WScript.Shell
foreach ($link in $links) {
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = (Get-Command powershell.exe).Source
    $shortcut.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $launcher + '"'
    $shortcut.WorkingDirectory = $repoRoot
    $shortcut.IconLocation = $icon + ',0'
    $shortcut.Description = 'Assetto Corsa AI — centre de contrôle'
    $shortcut.Save()
    Write-Output $link
}
