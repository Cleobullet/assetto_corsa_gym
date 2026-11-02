#Requires -Version 5.1
<#
.SYNOPSIS
    Surveille les processus Assetto Corsa et affiche leur origine (AI / Steam / inconnu).
#>
[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 90,
    [int]$PollMilliseconds = 500
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
} catch {
}

function Get-NormalizedPath([string]$Path) {
    if (-not $Path) { return $null }
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Get-ProcessExecutablePath {
    param([int]$ProcessId)

    $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if ($cim -and $cim.ExecutablePath) {
        return Get-NormalizedPath $cim.ExecutablePath
    }

    try {
        $proc = Get-Process -Id $ProcessId -ErrorAction Stop
        if ($proc.MainModule -and $proc.MainModule.FileName) {
            return Get-NormalizedPath $proc.MainModule.FileName
        }
    } catch {
    }

    return $null
}

function Test-PathIsUnderInstall {
    param(
        [string]$ExecutablePath,
        [string]$InstallRoot
    )
    $exe = Get-NormalizedPath $ExecutablePath
    $root = Get-NormalizedPath $InstallRoot
    if (-not $exe -or -not $root) { return $false }
    return $exe.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $exe.Equals($root, [StringComparison]::OrdinalIgnoreCase)
}

function Get-InstallSource {
    param(
        [string]$ExecutablePath,
        [string]$AiRoot,
        [string]$SteamRoot
    )
    if (-not $ExecutablePath) { return 'Inconnu (chemin illisible)' }
    if (Test-PathIsUnderInstall -ExecutablePath $ExecutablePath -InstallRoot $AiRoot) { return 'AI' }
    if (Test-PathIsUnderInstall -ExecutablePath $ExecutablePath -InstallRoot $SteamRoot) { return 'Steam' }
    return 'Inconnu'
}

function Write-WatchLog {
    param([string]$Message, [string]$LogFile)
    $line = "$(Get-Date -Format o) | $Message"
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'Get-AcLocalPaths.ps1')
$paths = Get-AcLocalPaths

$aiRoot = $paths.AiInstallPath
$steamRoot = $paths.SteamInstallPath
$logDir = Join-Path $paths.RepoRoot 'ac_launch_logs'
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$logFile = Join-Path $logDir 'process_watch.log'

$processNames = @('acs', 'AssettoCorsa', 'acs_x86')
$seen = @{}
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$aiConfirmed = $false
$steamDetected = $false

Write-Host ''
Write-Host 'Surveillance Assetto Corsa (max' $TimeoutSeconds 's)'
Write-Host 'AI   :' $aiRoot
Write-Host 'Steam:' $steamRoot
Write-Host 'Log  :' $logFile
Write-Host ''

Write-WatchLog -Message "START timeout=${TimeoutSeconds}s ai=$aiRoot steam=$steamRoot" -LogFile $logFile

while ((Get-Date) -lt $deadline) {
    foreach ($name in $processNames) {
        Get-Process -Name $name -ErrorAction SilentlyContinue | ForEach-Object {
            $procId = $_.Id
            if ($seen.ContainsKey($procId)) { return }

            $exePath = Get-ProcessExecutablePath -ProcessId $procId
            $source = Get-InstallSource -ExecutablePath $exePath -AiRoot $aiRoot -SteamRoot $steamRoot
            $seen[$procId] = $true

            $line = "PID=$procId Name=$name.exe Path=$exePath Source=$source"
            Write-Host $line
            Write-WatchLog -Message $line -LogFile $logFile

            if ($name -eq 'acs' -and $source -eq 'AI') { $aiConfirmed = $true }
            if ($source -eq 'Steam') { $steamDetected = $true }
        }
    }

    if ($aiConfirmed) {
        Write-Host ''
        Write-Host '[OK] acs.exe confirme depuis la copie AI.'
        Write-WatchLog -Message 'RESULT OK acs.exe from AI' -LogFile $logFile
        exit 0
    }

    if ($steamDetected) {
        Write-Host ''
        Write-Host '[ERREUR] Processus Steam detecte. Arretez et verifiez la racine Content Manager.'
        Write-WatchLog -Message 'RESULT FAIL Steam process detected' -LogFile $logFile
        exit 2
    }

    Start-Sleep -Milliseconds $PollMilliseconds
}

Write-Host ''
Write-Host '[TIMEOUT] Aucun acs.exe AI detecte dans le delai.'
Write-WatchLog -Message 'RESULT TIMEOUT no AI acs.exe' -LogFile $logFile
exit 1
