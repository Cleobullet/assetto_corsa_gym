#Requires -Version 5.1
<#
.SYNOPSIS
    Compare les reglages camera AC entre Documents, captures, backups et profils workers.
.EXAMPLE
    .\scripts\Audit-AI-Camera-State.ps1
    .\scripts\Audit-AI-Camera-State.ps1 -ReferenceCapture 20260622_220013
#>
[CmdletBinding()]
param(
    [string]$ReferenceCapture = '20260622_220013',
    [string]$RepoRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir 'Get-AcLocalPaths.ps1')
. (Join-Path $ScriptDir 'Ac-AiSessionCommon.ps1')

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
}
$paths = Get-AcLocalPaths -RepoRoot $RepoRoot
$DocsCfg = Join-Path $paths.AcDocumentsPath 'cfg'

function Get-CameraCandidateFiles {
    param([string]$CfgRoot)
    if (-not (Test-Path -LiteralPath $CfgRoot)) { return @() }
    $patterns = @('camera*.ini', '*onboard*.ini', '*view*.ini', 'video.ini', 'controls.ini')
    $found = @{}
    foreach ($pat in $patterns) {
        foreach ($f in @(Get-ChildItem -LiteralPath $CfgRoot -Filter $pat -File -ErrorAction SilentlyContinue)) {
            $found[$f.Name] = $f.FullName
        }
    }
    return @($found.Values | Sort-Object -Unique)
}

function Get-IniCameraRows {
    param([string]$Path)
    $rows = @()
    if (-not (Test-Path -LiteralPath $Path)) { return $rows }
    $section = ''
    $fileName = Split-Path -Leaf $Path
    foreach ($rawLine in @(Get-Content -LiteralPath $Path)) {
        $trim = $rawLine.Trim()
        if ($trim -match '^\[(.+)\]\s*$') {
            $section = $Matches[1]
            continue
        }
        if ($trim -match '^([^=;#]+)=(.*)$') {
            $key = $Matches[1].Trim()
            $val = (($Matches[2] -split ';')[0]).Trim()
            $isCam = $false
            if ($fileName -match 'camera|onboard|view') { $isCam = $true }
            if ($key -match 'MODE|FOV|CAMERA|VIEW|GLANCE|DRIVABLE') { $isCam = $true }
            if ($section -match 'CAMERA|LAST_CAMERA|MODE') { $isCam = $true }
            if ($key -eq 'ACTION_CHANGE_CAMERA' -or $section -eq 'ACTION_CHANGE_CAMERA') { $isCam = $true }
            if ($section -match 'GLANCE') { $isCam = $true }
            if ($isCam) {
                $rows += [pscustomobject]@{
                    File    = $fileName
                    Section = $section
                    Key     = $key
                    Value   = $val
                }
            }
        }
    }
    return $rows
}

function Get-LatestDocumentBackup {
    param([string]$ProfilesRoot)
    $backups = @(Get-ChildItem -LiteralPath (Join-Path $ProfilesRoot 'backups') -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^documents_' } |
        Sort-Object Name -Descending)
    if ($backups.Count -gt 0) { return $backups[0].FullName }
    return $null
}

$sources = [ordered]@{}
$sources['Documents (live)'] = $DocsCfg
$sources['Profil AI'] = Join-Path $RepoRoot 'ac_profiles\ai\cfg'
$sources['Worker 0'] = Join-Path $RepoRoot 'ac_profiles\workers\worker_0\cfg'
$sources['Worker 1'] = Join-Path $RepoRoot 'ac_profiles\workers\worker_1\cfg'

$refPath = Join-Path $RepoRoot ('ac_profiles\captures\' + $ReferenceCapture + '\cfg')
if (Test-Path -LiteralPath $refPath) {
    $sources['Capture ' + $ReferenceCapture] = $refPath
}

$latestBackup = Get-LatestDocumentBackup -ProfilesRoot (Join-Path $RepoRoot 'ac_profiles')
if ($latestBackup) {
    $sources['Dernier backup Documents'] = Join-Path $latestBackup 'cfg'
}

$allRows = @()
$filePresence = @{}
foreach ($label in $sources.Keys) {
    $cfgRoot = $sources[$label]
    $files = Get-CameraCandidateFiles -CfgRoot $cfgRoot
    foreach ($f in $files) {
        $rel = Split-Path -Leaf $f
        if (-not $filePresence.ContainsKey($rel)) {
            $filePresence[$rel] = @{}
        }
        $filePresence[$rel][$label] = $true
        foreach ($row in @(Get-IniCameraRows -Path $f)) {
            $allRows += [pscustomobject]@{
                Source  = $label
                File    = $row.File
                Section = $row.Section
                Key     = $row.Key
                Value   = $row.Value
            }
        }
    }
}

Write-Host '=== Audit caméra AC ==='
Write-Host ('Documents: ' + $paths.AcDocumentsPath)
Write-Host ''

Write-Host '--- Fichiers camera (presence) ---'
foreach ($rel in @($filePresence.Keys | Sort-Object)) {
    $present = @($sources.Keys | Where-Object { $filePresence[$rel].ContainsKey($_) })
    $absent = @($sources.Keys | Where-Object { -not $filePresence[$rel].ContainsKey($_) })
    Write-Host ($rel + ':')
    Write-Host ('  present: ' + ($present -join ', '))
    if ($absent.Count -gt 0) {
        Write-Host ('  absent : ' + ($absent -join ', '))
    }
}

Write-Host ''
Write-Host '--- Cles camera par source ---'
$groups = $allRows | Group-Object -Property File, Section, Key
foreach ($g in $groups) {
    $distinct = @($g.Group | Select-Object -ExpandProperty Value -Unique)
    if ($distinct.Count -le 1) { continue }
    $parts = $g.Name -split ', ', 3
    Write-Host ('DIFF ' + $parts[0] + ' [' + $parts[1] + '] ' + $parts[2])
    foreach ($item in $g.Group) {
        Write-Host ('  ' + $item.Source + ' = ' + $item.Value)
    }
}

Write-Host ''
Write-Host '--- Valeurs critiques ---'
$critical = @(
    @{ File = 'camera_manager.ini'; Section = 'LAST_CAMERA'; Key = 'MODE' },
    @{ File = 'camera_manager.ini'; Section = 'LAST_CAMERA'; Key = 'DRIVABLE_MODE' },
    @{ File = 'video.ini'; Section = 'CAMERA'; Key = 'MODE' }
)
foreach ($c in $critical) {
    Write-Host ($c.File + ' [' + $c.Section + '] ' + $c.Key + ':')
    foreach ($label in $sources.Keys) {
        $path = Join-Path $sources[$label] $c.File
        if (-not (Test-Path -LiteralPath $path)) {
            Write-Host ('  ' + $label + ' = (fichier absent)')
            continue
        }
        $val = Get-IniValue -Path $path -Section $c.Section -Key $c.Key
        if (-not $val) { $val = '(vide)' }
        Write-Host ('  ' + $label + ' = ' + $val)
    }
}

$liveMode = Get-IniValue -Path (Join-Path $DocsCfg 'camera_manager.ini') -Section 'LAST_CAMERA' -Key 'MODE'
Write-Host ''
if ($liveMode -eq '2') {
    Write-Host 'NOTE: cfg/camera_manager.ini [LAST_CAMERA] MODE=2 (vue laterale).'
    Write-Host 'Utilisez Capture-AI-Presentation-Camera.ps1 pour capturer la vue 3e personne desiree.'
} elseif ($liveMode -eq '0') {
    Write-Host 'Documents: camera_manager MODE=0 (onboard).'
} else {
    Write-Host ('Documents: camera_manager MODE=' + [string]$liveMode)
}

Write-Host ''
Write-Host 'Audit-AI-Camera-State: OK'
