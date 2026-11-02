# Launch the WPF control panel from the stable build folder.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Root "ac_workers\control_center.py"))) {
    $Root = $PSScriptRoot
    if (-not (Test-Path (Join-Path $Root "ac_workers\control_center.py"))) {
        $Root = Split-Path -Parent $PSScriptRoot
    }
}

$Project = Join-Path $Root "desktop\AcAiControl\AcAiControl.csproj"
$SourceDir = Join-Path $Root "desktop\AcAiControl"
$CompileOut = Join-Path $Root "desktop\AcAiControl\bin\Release\net8.0-windows"
$StableDir = Join-Path $Root "desktop\build-stop"
$Exe = Join-Path $StableDir "AcAiControl.exe"

$needsBuild = -not (Test-Path -LiteralPath (Join-Path $CompileOut "AcAiControl.exe"))
if (-not $needsBuild) {
    $exeDate = (Get-Item -LiteralPath (Join-Path $CompileOut "AcAiControl.exe")).LastWriteTimeUtc
    $newerSource = Get-ChildItem -LiteralPath $SourceDir -File -Recurse |
        Where-Object {
            $_.FullName -notmatch '[\\/](bin|obj)[\\/]' -and
            $_.Extension -in @('.cs', '.xaml', '.csproj', '.ico') -and
            $_.LastWriteTimeUtc -gt $exeDate
        } |
        Select-Object -First 1
    $needsBuild = $null -ne $newerSource
}

if ($needsBuild) {
    Write-Host "Building AcAiControl..."
    dotnet build $Project -c Release | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Compilation du centre de contrôle échouée.' }
}

New-Item -ItemType Directory -Force -Path $StableDir | Out-Null
Copy-Item -Path (Join-Path $CompileOut '*') -Destination $StableDir -Recurse -Force

if (-not (Test-Path -LiteralPath $Exe)) {
    throw "Executable introuvable: $Exe"
}

Start-Process -FilePath $Exe -WorkingDirectory $Root
