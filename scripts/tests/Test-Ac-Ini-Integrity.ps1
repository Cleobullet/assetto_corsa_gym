#Requires -Version 5.1
<#
.SYNOPSIS
    Compare INI Documents actifs vs references saines; reparation optionnelle.
#>
[CmdletBinding()]
param(
    [switch]$Repair,
    [string]$RepoRoot,
    [string]$HealthyCapture = '20260622_220013'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
. (Join-Path $ScriptsRoot 'Get-AcLocalPaths.ps1')
. (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1')

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..\..')).Path
}

$paths = Get-AcLocalPaths -RepoRoot $RepoRoot
$activeRoot = Join-Path $paths.AcDocumentsPath 'cfg'
$issues = @()

function Get-IniEntries {
    param([Parameter(Mandatory)][string]$Path)
    $rows = @()
    if (-not (Test-Path -LiteralPath $Path)) { return $rows }
    $section = ''
    foreach ($rawLine in @(Get-Content -LiteralPath $Path)) {
        $trim = $rawLine.Trim()
        if ($trim -match '^\[(.+)\]\s*$') {
            $section = $Matches[1]
            continue
        }
        if ($trim -match '^([^=;#]+)=(.*)$') {
            $val = ($Matches[2] -split ';')[0].Trim()
            $rows += [pscustomobject]@{
                Section = $section
                Key     = $Matches[1].Trim()
                Value   = $val
            }
        }
    }
    return $rows
}

function Test-IsIntegerString {
    param([string]$Value)
    return ($Value -match '^-?\d+$')
}

function Get-HealthyCfgRoots {
    param([string]$Repo, [string]$CaptureId)
    $roots = @()
    $ai = Join-Path $Repo 'ac_profiles\ai\cfg'
    if (Test-Path -LiteralPath $ai) { $roots += $ai }
    $backups = @(Get-ChildItem -LiteralPath (Join-Path $Repo 'ac_profiles\backups') -Directory -Filter 'documents_*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending)
    foreach ($b in $backups) {
        $bp = Join-Path $b.FullName 'cfg'
        if (Test-Path -LiteralPath $bp) { $roots += $bp }
    }
    $cap = Join-Path $Repo ('ac_profiles\captures\' + $CaptureId + '\cfg')
    if (Test-Path -LiteralPath $cap) { $roots += $cap }
    return @($roots | Select-Object -Unique)
}

function Find-HealthyValue {
    param(
        [string[]]$HealthyRoots,
        [string]$FileName,
        [string]$Section,
        [string]$Key
    )
    foreach ($root in $HealthyRoots) {
        $path = Join-Path $root $FileName
        if (-not (Test-Path -LiteralPath $path)) { continue }
        foreach ($row in @(Get-IniEntries -Path $path)) {
            if ($row.Section -eq $Section -and $row.Key -eq $Key) {
                return $row.Value
            }
        }
    }
    return $null
}

$healthyRoots = @(Get-HealthyCfgRoots -Repo $RepoRoot -CaptureId $HealthyCapture)
if ((Get-CollectionCount $healthyRoots) -eq 0) {
    throw 'Aucune reference INI saine trouvee.'
}

$iniNames = @('video.ini', 'race.ini', 'controls.ini', 'assists.ini', 'gameplay.ini')
$cameraFiles = @(Get-ChildItem -LiteralPath $activeRoot -Filter 'camera*.ini' -File -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
$iniNames = @($iniNames + $cameraFiles | Select-Object -Unique)

foreach ($iniName in $iniNames) {
    $activePath = Join-Path $activeRoot $iniName
    if (-not (Test-Path -LiteralPath $activePath)) { continue }
    foreach ($activeRow in @(Get-IniEntries -Path $activePath)) {
        $healthyVal = Find-HealthyValue -HealthyRoots $healthyRoots -FileName $iniName `
            -Section $activeRow.Section -Key $activeRow.Key
        if ($null -eq $healthyVal) { continue }
        $activeVal = $activeRow.Value
        $healthyIsInt = Test-IsIntegerString $healthyVal
        if (-not $healthyIsInt) { continue }
        $activeBad = ($activeVal -eq '') -or (-not (Test-IsIntegerString $activeVal))
        if (-not $activeBad) { continue }
        $issues += [pscustomobject]@{
            File     = $iniName
            Section  = $activeRow.Section
            Key      = $activeRow.Key
            Active   = $activeVal
            Healthy  = $healthyVal
            Path     = $activePath
        }
    }
}

if ((Get-CollectionCount $issues) -eq 0) {
    Write-Host 'Test-Ac-Ini-Integrity: OK (aucune corruption detectee)'
    exit 0
}

Write-Host 'Test-Ac-Ini-Integrity: ISSUES'
foreach ($i in $issues) {
    Write-Host ('  ' + $i.File + ' [' + $i.Section + '] ' + $i.Key + " active='" + $i.Active + "' healthy='" + $i.Healthy + "'")
}

if (-not $Repair) {
    exit 1
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$backupDir = Join-Path $RepoRoot ('ac_profiles\backups\ini_repair_' + $stamp)
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
foreach ($iniName in @($issues | ForEach-Object { $_.File } | Select-Object -Unique)) {
    $src = Join-Path $activeRoot $iniName
    $dst = Join-Path $backupDir $iniName
    $dstParent = Split-Path -Parent $dst
    if ($dstParent -and -not (Test-Path -LiteralPath $dstParent)) {
        New-Item -ItemType Directory -Path $dstParent -Force | Out-Null
    }
    Copy-Item -LiteralPath $src -Destination $dst -Force
}
Write-Host ('Backup: ' + $backupDir)

foreach ($i in $issues) {
    Set-IniValue -Path $i.Path -Section $i.Section -Key $i.Key -Value $i.Healthy
    Write-Host ('  repare: ' + $i.File + ' [' + $i.Section + '] ' + $i.Key + '=' + $i.Healthy)
}

Write-Host 'Test-Ac-Ini-Integrity: REPAIRED'
exit 0
