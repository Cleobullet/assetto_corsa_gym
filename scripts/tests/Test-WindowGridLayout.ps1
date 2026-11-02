#Requires -Version 5.1
<#
.SYNOPSIS
    Tests hors ligne du calcul de grille fenetres AC (1-6 workers).
#>
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
. (Join-Path $ScriptsRoot 'Ac-AiSessionCommon.ps1')

$fail = @()
$wa1920 = @{ X = 0; Y = 0; Width = 1920; Height = 1080 }
$wa2560 = @{ X = 0; Y = 0; Width = 2560; Height = 1440 }

$expectedGrid = @{
    1 = @{ Cols = 1; Rows = 1 }
    2 = @{ Cols = 2; Rows = 1 }
    3 = @{ Cols = 2; Rows = 2 }
    4 = @{ Cols = 2; Rows = 2 }
    5 = @{ Cols = 3; Rows = 2 }
    6 = @{ Cols = 3; Rows = 2 }
}

foreach ($n in 1..6) {
    $g = Get-AcWindowGridDimensions -WorkerCount $n
    $exp = $expectedGrid[$n]
    if ($g.Cols -ne $exp.Cols -or $g.Rows -ne $exp.Rows) {
        $fail += ('grid ' + $n + ' workers: attendu ' + $exp.Cols + 'x' + $exp.Rows + ' obtenu ' + $g.Cols + 'x' + $g.Rows)
    }
}

function Test-CellEven16x9($cell, [string]$label) {
    if (($cell.renderWidth % 2) -ne 0 -or ($cell.renderHeight % 2) -ne 0) {
        return ($label + ': render non pair ' + $cell.renderWidth + 'x' + $cell.renderHeight)
    }
    if (($cell.renderWidth % 16) -ne 0 -or ($cell.renderHeight % 16) -ne 0) {
        return ($label + ': render non aligne 16px ' + $cell.renderWidth + 'x' + $cell.renderHeight)
    }
    $ratio = $cell.renderWidth / [double]$cell.renderHeight
    if ([math]::Abs($ratio - (16.0 / 9.0)) -gt 0.09) {
        return ($label + ': ratio 16:9 hors tolerance ' + $cell.renderWidth + 'x' + $cell.renderHeight)
    }
    if ($cell.outerWidth -gt $cell.screenWidth -or $cell.outerHeight -gt $cell.screenHeight) {
        return ($label + ': outer depasse cellule ' + $cell.outerWidth + 'x' + $cell.outerHeight)
    }
    if (Initialize-AcWindowPlacementNative) {
        $fit = [AcWindowPlacementNative]::OuterToClient($cell.outerWidth, $cell.outerHeight)
        if ($fit.Width -lt $cell.renderWidth -or $fit.Height -lt $cell.renderHeight) {
            return ($label + ': render depasse client outer ' + $fit.Width + 'x' + $fit.Height)
        }
    }
    return $null
}

$layout6 = Get-AcWindowGridLayout -WorkerCount 6 -MonitorIndex 0 -WindowGap 0 `
    -WorkingAreaOverride $wa1920
if ($layout6.cols -ne 3 -or $layout6.rows -ne 2) {
    $fail += ('6 workers layout: attendu 3x2 obtenu ' + $layout6.cols + 'x' + $layout6.rows)
}
$cell0 = $layout6.cells[0]
$chk = Test-CellEven16x9 $cell0 '1920x1080 6 workers'
if ($chk) { $fail += $chk }
if ($cell0.renderWidth -lt 600 -or $cell0.renderHeight -lt 330) {
    $fail += ('1920x1080 6 workers render trop petit: ' + $cell0.renderWidth + 'x' + $cell0.renderHeight)
}

$layout1 = Get-AcWindowGridLayout -WorkerCount 1 -MonitorIndex 0 -WindowGap 0 `
    -WindowLayout Legacy -WorkingAreaOverride $wa2560
$cell1 = $layout1.cells[0]
if ($cell1.renderWidth -ne 1280 -or $cell1.renderHeight -ne 720) {
    $fail += ('2560x1440 1 worker Legacy: attendu 1280x720 obtenu ' + $cell1.renderWidth + 'x' + $cell1.renderHeight)
}

$layout2 = Get-AcWindowGridLayout -WorkerCount 2 -MonitorIndex 0 -WindowGap 0 `
    -WindowLayout Legacy -WorkingAreaOverride $wa2560
$cell2 = $layout2.cells[0]
$chk2 = Test-CellEven16x9 $cell2 '2560x1440 2 workers'
if ($chk2) { $fail += $chk2 }
if ($cell2.renderWidth -ne 1264 -or $cell2.renderHeight -ne 704) {
    $fail += ('2560x1440 2 workers: attendu 1264x704 obtenu ' + $cell2.renderWidth + 'x' + $cell2.renderHeight)
}

$layout6hd = Get-AcWindowGridLayout -WorkerCount 6 -MonitorIndex 0 -WindowGap 0 `
    -WindowLayout Legacy -WorkingAreaOverride $wa2560
$cell6hd = $layout6hd.cells[0]
$chk6 = Test-CellEven16x9 $cell6hd '2560x1440 6 workers'
if ($chk6) { $fail += $chk6 }
if ($cell6hd.renderWidth -ne 832 -or $cell6hd.renderHeight -ne 464) {
    $fail += ('2560x1440 6 workers: attendu 832x464 obtenu ' + $cell6hd.renderWidth + 'x' + $cell6hd.renderHeight)
}

$res = Get-AcMaxResolution16x9 -MaxWidth 640 -MaxHeight 540
if ($res.Width -ne 640 -or $res.Height -ne 360) {
    $fail += ('16:9 640x540: attendu 640x360 obtenu ' + $res.Width + 'x' + $res.Height)
}

if ($fail.Count -gt 0) {
    $fail | ForEach-Object { Write-Host ('FAIL: ' + $_) }
    exit 1
}
Write-Host 'Test-WindowGridLayout: OK'
exit 0
