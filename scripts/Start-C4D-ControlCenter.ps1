#Requires -Version 5.1
<#
.SYNOPSIS
    Lance le C4D Control Center: un seul serveur central + dashboard + console.
#>
[CmdletBinding()]
param(
    [int]$Port = 24700
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir '..')).Path
Set-Location $RepoRoot

function Resolve-C4DPython {
    $dir = $RepoRoot
    while ($dir) {
        $candidate = Join-Path $dir '.venv-acgym\Scripts\python.exe'
        if (Test-Path -LiteralPath $candidate) { return $candidate }
        $parent = Split-Path $dir -Parent
        if (-not $parent -or $parent -eq $dir) { break }
        $dir = $parent
    }
    throw 'Python venv introuvable: .venv-acgym\Scripts\python.exe'
}
$VenvPython = Resolve-C4DPython

$env:PYTHONPATH = "$RepoRoot;$RepoRoot\assetto_corsa_gym"
$HostAddr = '127.0.0.1'
$C4DServerDir = Join-Path $RepoRoot 'ac_workers\.c4d_server'
$ServerStdoutLog = Join-Path $C4DServerDir 'server_stdout.log'
$ServerStderrLog = Join-Path $C4DServerDir 'server_stderr.log'

function ConvertTo-C4DWindowsArgument {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Text
    )
    if ($Text -eq '') { return '""' }
    if ($Text -notmatch '[\s\x22]') { return $Text }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $chars = $Text.ToCharArray()
    $i = 0
    while ($i -lt $chars.Length) {
        $backslashes = 0
        while ($i -lt $chars.Length -and $chars[$i] -eq '\') {
            $backslashes++
            $i++
        }
        if ($i -eq $chars.Length) {
            [void]$sb.Append('\' * (2 * $backslashes))
        } elseif ($chars[$i] -eq '"') {
            [void]$sb.Append('\' * (2 * $backslashes + 1))
            [void]$sb.Append('"')
            $i++
        } else {
            if ($backslashes -gt 0) { [void]$sb.Append('\' * $backslashes) }
            [void]$sb.Append($chars[$i])
            $i++
        }
    }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function ConvertTo-C4DProcessArguments {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList
    )
    return (($ArgumentList | ForEach-Object { ConvertTo-C4DWindowsArgument -Text $_ }) -join ' ')
}

function Get-C4DLogTail {
    param(
        [string]$Path,
        [int]$MaxChars = 8000
    )
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $text = Get-Content -LiteralPath $Path -Raw -ErrorAction SilentlyContinue
    if ($null -eq $text) { return '' }
    if ($text.Length -le $MaxChars) { return $text }
    return $text.Substring($text.Length - $MaxChars)
}

function Format-C4DServerStartFailure {
    param(
        [System.Diagnostics.Process]$Process,
        [string]$CommandLine,
        [string]$StdoutLog,
        [string]$StderrLog
    )
    $stderr = Get-C4DLogTail -Path $StderrLog
    $stdout = Get-C4DLogTail -Path $StdoutLog
    $msg = 'C4D server exited early code=' + $Process.ExitCode + [Environment]::NewLine +
        'command=' + $CommandLine
    if ($stderr) {
        $msg += [Environment]::NewLine + 'stderr:' + [Environment]::NewLine + $stderr
    }
    if ($stdout) {
        $msg += [Environment]::NewLine + 'stdout:' + [Environment]::NewLine + $stdout
    }
    return $msg
}

function New-C4DProcessStart {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentTokens,
        [string]$WindowStyle = 'Normal',
        [string]$StdoutLog = '',
        [string]$StderrLog = ''
    )
    $start = @{
        FilePath = $VenvPython
        ArgumentList = (ConvertTo-C4DProcessArguments -ArgumentList $ArgumentTokens)
        WorkingDirectory = $RepoRoot
        PassThru = $true
    }
    if ($WindowStyle) { $start['WindowStyle'] = $WindowStyle }
    if ($StdoutLog) {
        if (Test-Path -LiteralPath $StdoutLog) { Remove-Item -LiteralPath $StdoutLog -Force }
        $start['RedirectStandardOutput'] = $StdoutLog
    }
    if ($StderrLog) {
        if (Test-Path -LiteralPath $StderrLog) { Remove-Item -LiteralPath $StderrLog -Force }
        $start['RedirectStandardError'] = $StderrLog
    }
    return $start
}

function Get-C4DClientArgs {
    param(
        [string]$Mode,
        [string]$Token
    )
    return @(
        '-m', 'ac_workers.control_center', $Mode,
        '--host', $HostAddr,
        '--port', [string]$Port,
        '--repo-root', $RepoRoot,
        '--session-token', $Token
    )
}

function Start-C4DClients {
    param(
        [string]$Token,
        [string]$SessionFile
    )
    $dashStart = New-C4DProcessStart -ArgumentTokens (Get-C4DClientArgs -Mode 'dashboard' -Token $Token)
    $consoleStart = New-C4DProcessStart -ArgumentTokens (Get-C4DClientArgs -Mode 'console' -Token $Token)
    $dashProc = Start-Process @dashStart
    $consoleProc = Start-Process @consoleStart
    return @($dashProc, $consoleProc)
}

if (-not (Test-Path -LiteralPath $C4DServerDir)) {
    New-Item -ItemType Directory -Path $C4DServerDir -Force | Out-Null
}

$PrepareArgs = @(
    '-m', 'ac_workers.c4d_server_lock', 'prepare',
    '--repo-root', $RepoRoot,
    '--host', $HostAddr,
    '--port', [string]$Port
)
$prepRaw = & $VenvPython @PrepareArgs
if ($LASTEXITCODE -ne 0) { throw $prepRaw }
$prep = $prepRaw | ConvertFrom-Json
$token = [string]$prep.token
$sessionFile = [string]$prep.session_file
if (-not $token) { throw 'C4D prepare: token missing' }

if ($prep.action -eq 'reuse') {
    Write-Host ('C4D server reused pid=' + $prep.server_pid + ' token=' + $token)
    $clients = Start-C4DClients -Token $token -SessionFile $sessionFile
    $UpdateArgs = @(
        '-m', 'ac_workers.c4d_server_lock', 'update-clients',
        '--repo-root', $RepoRoot,
        '--launcher-pid', [string]$PID,
        '--dashboard-pid', [string]$clients[0].Id,
        '--console-pid', [string]$clients[1].Id
    )
    & $VenvPython @UpdateArgs | Out-Null
    exit 0
}

$readyFile = Join-Path $env:TEMP ('c4d_server_ready_{0}.flag' -f $PID)
if (Test-Path -LiteralPath $readyFile) { Remove-Item -LiteralPath $readyFile -Force }

$serverArgTokens = @(
    '-m', 'ac_workers.control_center', 'server',
    '--host', $HostAddr,
    '--port', [string]$Port,
    '--repo-root', $RepoRoot,
    '--session-token', $token,
    '--session-file', $sessionFile,
    '--ready-file', $readyFile
)
$serverStart = New-C4DProcessStart `
    -ArgumentTokens $serverArgTokens `
    -WindowStyle 'Hidden' `
    -StdoutLog $ServerStdoutLog `
    -StderrLog $ServerStderrLog
$serverArgLine = [string]$serverStart['ArgumentList']
$serverProc = Start-Process @serverStart

$deadline = (Get-Date).AddSeconds(20)
$readyOk = $false
while (-not $readyOk) {
    if ($serverProc.HasExited) {
        throw (Format-C4DServerStartFailure -Process $serverProc -CommandLine $serverArgLine -StdoutLog $ServerStdoutLog -StderrLog $ServerStderrLog)
    }
    if ((Get-Date) -gt $deadline) {
        try { Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue } catch { }
        throw (Format-C4DServerStartFailure -Process $serverProc -CommandLine $serverArgLine -StdoutLog $ServerStdoutLog -StderrLog $ServerStderrLog)
    }
    if (Test-Path -LiteralPath $readyFile) {
        try {
            $ready = Get-Content -LiteralPath $readyFile -Raw | ConvertFrom-Json
            if ($ready.token -eq $token -and [int]$ready.server_pid -gt 0) {
                $readyOk = $true
            }
        } catch { }
    }
    if (-not $readyOk) { Start-Sleep -Milliseconds 100 }
}

Write-Host ('C4D server started pid=' + $ready.server_pid + ' token=' + $token)
$clients = Start-C4DClients -Token $token -SessionFile $sessionFile
$UpdateArgs = @(
    '-m', 'ac_workers.c4d_server_lock', 'update-clients',
    '--repo-root', $RepoRoot,
    '--launcher-pid', [string]$PID,
    '--dashboard-pid', [string]$clients[0].Id,
    '--console-pid', [string]$clients[1].Id
)
& $VenvPython @UpdateArgs | Out-Null

$exitCode = 0
try {
    $serverProc.WaitForExit()
    $exitCode = $serverProc.ExitCode
} finally {
    if (Test-Path -LiteralPath $readyFile) { Remove-Item -LiteralPath $readyFile -Force -ErrorAction SilentlyContinue }
}
exit $exitCode
