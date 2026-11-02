#Requires -Version 5.1
<#
.SYNOPSIS
    Installe (1 seule fois) le janitor vJoy privilegie comme tache planifiee SYSTEM.
.DESCRIPTION
    Cree une tache planifiee "ACGym-vJoyJanitor" qui lance VjoyJanitor-Service.ps1 en
    compte SYSTEM, niveau le plus eleve, au demarrage de la machine ET tout de suite.
    Apres cette installation (un seul prompt UAC), le Control Center peut declencher
    des resets vJoy cibles SANS aucun prompt UAC.
.PARAMETER Uninstall
    Supprime la tache planifiee et arrete le service.
#>
[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$TaskName = 'ACGym-vJoyJanitor'

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

if (-not (Test-IsAdmin)) {
    Write-Host 'Elevation requise (UAC)...'
    $self = $MyInvocation.MyCommand.Path
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $self)
    if ($Uninstall) { $argList += '-Uninstall' }
    Start-Process powershell -Verb RunAs -Wait -ArgumentList $argList
    exit $LASTEXITCODE
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $scriptDir '..')).Path
$service = Join-Path $scriptDir 'VjoyJanitor-Service.ps1'

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host ('Tache ' + $TaskName + ' supprimee.') -ForegroundColor Green
    } else {
        Write-Host ('Tache ' + $TaskName + ' introuvable (rien a faire).') -ForegroundColor DarkYellow
    }
    Get-Process powershell -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and ($_.CommandLine -match 'VjoyJanitor-Service') } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    exit 0
}

if (-not (Test-Path -LiteralPath $service)) {
    Write-Host ('ERREUR: service introuvable: ' + $service) -ForegroundColor Red
    exit 2
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host ('Tache ' + $TaskName + ' existe deja -> recreation.') -ForegroundColor DarkYellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$argLine = ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $service + '" -RepoRoot "' + $repoRoot + '"')
$action = New-ScheduledTaskAction -Execute $psExe -Argument $argLine
$triggerBoot = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggerBoot -Principal $principal -Settings $settings -Description 'ACGym privileged vJoy janitor (driver reset on request)' | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host ('Tache ' + $TaskName + ' installee et demarree (compte SYSTEM).') -ForegroundColor Green
Write-Host 'Verification du heartbeat...' -ForegroundColor Cyan
$hb = Join-Path $repoRoot 'ac_workers\.vjoy_janitor\heartbeat.json'
$deadline = (Get-Date).AddSeconds(20)
$alive = $false
while ((Get-Date) -lt $deadline) {
    if (Test-Path -LiteralPath $hb) { $alive = $true; break }
    Start-Sleep -Milliseconds 500
}
if ($alive) {
    Write-Host 'OK: le janitor vJoy est actif.' -ForegroundColor Green
    exit 0
}
Write-Host 'ATTENTION: heartbeat non detecte (la tache peut mettre un instant a demarrer).' -ForegroundColor DarkYellow
exit 0
