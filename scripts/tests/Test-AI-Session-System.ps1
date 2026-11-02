#Requires -Version 5.1
<#
.SYNOPSIS
    Tests hors ligne des fonctions communes du workflow AI session.
#>
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
. (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1')

$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('ac_ai_session_test_' + [guid]::NewGuid().ToString('N'))
$failures = @()

function Assert-True {
    param([bool]$Condition, [string]$Name)
    if (-not $Condition) { $script:failures += $Name }
}

function Assert-Equal {
    param([string]$Expected, [string]$Actual, [string]$Name)
    if ([string]$Expected -ne [string]$Actual) {
        $script:failures += ($Name + ' (expected=' + [string]$Expected + ' actual=' + [string]$Actual + ')')
    }
}

function Assert-Count {
    param($Items, [int]$Expected, [string]$Name)
    $n = Get-CollectionCount $Items
    if ($n -ne $Expected) {
        $script:failures += ($Name + ' (count=' + $n + ' expected=' + $Expected + ')')
    }
}

try {
    New-Item -ItemType Directory -Path $TempRoot -Force | Out-Null
    $setupsDir = Join-Path $TempRoot 'savedsetups'
    New-Item -ItemType Directory -Path $setupsDir -Force | Out-Null

    $pythonIni = Join-Path $TempRoot 'python.ini'
    Set-Content -LiteralPath $pythonIni -Value @(
        '[other]'
        'FOO=bar'
        '[sensors_par]'
        'ACTIVE=0'
        '[sensors_par]'
        'ACTIVE=0'
    ) -Encoding UTF8

    Write-SensorsParPythonIni -TargetPath $pythonIni
    $content = @(Get-Content -LiteralPath $pythonIni)
    $sectionCount = @($content | Where-Object { $_.Trim() -eq '[sensors_par]' }).Count
    Assert-Equal '1' ([string]$sectionCount) 'single sensors_par section'
    Assert-Equal '1' (Get-IniValue -Path $pythonIni -Section 'sensors_par' -Key 'ACTIVE') 'ACTIVE=1'
    Assert-Equal 'bar' (Get-IniValue -Path $pythonIni -Section 'other' -Key 'FOO') 'other section preserved'

    $raceIni = Join-Path $TempRoot 'race.ini'
    Set-Content -LiteralPath $raceIni -Value @(
        '[RACE]'
        'MODEL=old'
        'CAR=old'
        'CARS=1'
        'TRACK=old'
        'CONFIG_TRACK=layout'
    ) -Encoding UTF8
    Set-AcRaceIniCarTrack -RaceIniPath $raceIni -Car 'lotus_exos_125' -Track 'monza'
    Assert-Equal 'lotus_exos_125' (Get-IniValue -Path $raceIni -Section 'RACE' -Key 'MODEL') 'race MODEL section'
    Assert-Equal 'monza' (Get-IniValue -Path $raceIni -Section 'RACE' -Key 'TRACK') 'race TRACK section'
    Assert-Equal '1' (Get-IniValue -Path $raceIni -Section 'RACE' -Key 'CARS') 'race CARS untouched'

    $emptyIni = Join-Path $TempRoot 'empty.ini'
    Set-IniValue -Path $emptyIni -Key 'BLANK' -Value '' -CreateFile
    Assert-Equal '' (Get-IniValue -Path $emptyIni -Key 'BLANK') 'empty value'

    Assert-Count (Get-VjoyPresetFiles -SavedSetupsDir $setupsDir) 0 'zero vjoy'
    Set-Content -LiteralPath (Join-Path $setupsDir 'wheel_vjoy.ini') -Value 'x=1' -Encoding UTF8
    Assert-Count (Get-VjoyPresetFiles -SavedSetupsDir $setupsDir) 1 'one vjoy'
    Set-Content -LiteralPath (Join-Path $setupsDir 'pad_vjoy.ini') -Value 'x=2' -Encoding UTF8
    Assert-Count (Get-VjoyPresetFiles -SavedSetupsDir $setupsDir) 2 'two vjoy'

    Assert-True (-not (Test-TrainingAlreadyRunning -RepoRoot $TempRoot)) 'training not running'
    $running = @(Get-AcRunningProcessNames)
    $runningCount = Get-CollectionCount $running
    Assert-True ($runningCount -ge 0) 'running processes array safe count'

    $boolTest = Test-TcpPortOpen -Port 59999
    Assert-True (($boolTest -eq $true) -or ($boolTest -eq $false)) 'predicate strict bool'

    $staticJson = '{"CarName":"lotus_exos_125","TrackName":"monza","TrackConfiguration":0}'
    $staticObj = $staticJson | ConvertFrom-Json
    Assert-True (Test-AcStaticInfoCarTrack -StaticInfo $staticObj) 'static_info car track match'

    Initialize-AcSendInputNative | Out-Null
    Release-StuckModifierKeys
    Assert-True $true 'Release-StuckModifierKeys callable'

    if ((Get-CollectionCount $failures) -eq 0) {
        Write-Host 'Test-AI-Session-System: OK'
        exit 0
    }
    Write-Host 'Test-AI-Session-System: FAIL'
    foreach ($f in $failures) { Write-Host ('  ' + $f) }
    exit 1
} finally {
    if (Test-Path -LiteralPath $TempRoot) {
        Remove-Item -LiteralPath $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
