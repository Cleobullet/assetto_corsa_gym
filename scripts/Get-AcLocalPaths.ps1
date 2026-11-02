#Requires -Version 5.1
Set-StrictMode -Version Latest

function Resolve-AcGymPython {
    param([string]$RepoRoot = '')
    $start = if ($RepoRoot) { Get-NormalizedPath $RepoRoot } else { Get-NormalizedPath (Join-Path $PSScriptRoot '..') }
    $dir = $start
    while ($dir) {
        $candidate = Join-Path $dir '.venv-acgym\Scripts\python.exe'
        if (Test-Path -LiteralPath $candidate) { return (Get-NormalizedPath $candidate) }
        $parent = Split-Path $dir -Parent
        if (-not $parent -or $parent -eq $dir) { break }
        $dir = $parent
    }
    throw 'Python venv introuvable: .venv-acgym\Scripts\python.exe'
}

function Get-NormalizedPath([string]$Path) {
    if (-not $Path) { return $null }
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Find-SteamInstallPath {
    $reg = Get-ItemProperty -Path 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue
    if ($reg -and $reg.SteamPath) {
        $libraries = @($reg.SteamPath)
        $vdf = Join-Path $reg.SteamPath 'steamapps\libraryfolders.vdf'
        if (Test-Path -LiteralPath $vdf) {
            $content = Get-Content -LiteralPath $vdf -Raw
            foreach ($m in [regex]::Matches($content, '"path"\s+"([^"]+)"')) {
                $p = $m.Groups[1].Value -replace '\\\\', '\'
                if (Test-Path -LiteralPath $p) { $libraries += $p }
            }
        }
        foreach ($lib in ($libraries | Select-Object -Unique)) {
            $candidate = Join-Path $lib 'steamapps\common\assettocorsa'
            if (Test-Path -LiteralPath (Join-Path $candidate 'AssettoCorsa.exe')) {
                return (Get-NormalizedPath $candidate)
            }
        }
    }
    $fallback = Join-Path ${env:ProgramFiles(x86)} 'Steam\steamapps\common\assettocorsa'
    if (Test-Path -LiteralPath (Join-Path $fallback 'AssettoCorsa.exe')) {
        return (Get-NormalizedPath $fallback)
    }
    return $null
}

function Find-AcDocumentsPath {
    foreach ($c in @(
        (Join-Path $env:USERPROFILE 'OneDrive\Documents\Assetto Corsa')
        (Join-Path $env:USERPROFILE 'Documents\Assetto Corsa')
        (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Assetto Corsa')
    )) {
        if (Test-Path -LiteralPath $c) {
            return (Get-NormalizedPath $c)
        }
    }
    return $null
}

function Get-AcLocalPaths {
    param(
        [string]$RepoRoot = '',
        [string]$ConfigPath = ''
    )

    $repo = if ($RepoRoot) {
        Get-NormalizedPath $RepoRoot
    } else {
        Get-NormalizedPath (Join-Path $PSScriptRoot '..')
    }

    if (-not $ConfigPath) {
        $ConfigPath = Join-Path $repo 'config\local-paths.json'
    }

    $cfg = $null
    if (Test-Path -LiteralPath $ConfigPath) {
        $cfg = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    }

    $steam = if ($cfg -and $cfg.steamInstallPath) {
        Get-NormalizedPath $cfg.steamInstallPath
    } else {
        Find-SteamInstallPath
    }

    $ai = if ($cfg -and $cfg.aiInstallPath) {
        Get-NormalizedPath $cfg.aiInstallPath
    } else {
        $candidate = Get-NormalizedPath (Join-Path $repo 'AssettoCorsa-AI\assettocorsa')
        if (Test-Path -LiteralPath (Join-Path $candidate 'AI_INSTANCE.txt')) {
            $candidate
        } else {
            $dir = $repo
            $found = $null
            while ($dir) {
                $try = Join-Path $dir 'AssettoCorsa-AI\assettocorsa'
                if (Test-Path -LiteralPath (Join-Path $try 'AI_INSTANCE.txt')) {
                    $found = Get-NormalizedPath $try
                    break
                }
                $parent = Split-Path $dir -Parent
                if (-not $parent -or $parent -eq $dir) { break }
                $dir = $parent
            }
            if ($found) { $found } else { $candidate }
        }
    }

    $docs = if ($cfg -and $cfg.acDocumentsPath) {
        Get-NormalizedPath $cfg.acDocumentsPath
    } else {
        Find-AcDocumentsPath
    }

    $contentManagerAi = $null
    if ($cfg) {
        $cmProp = $cfg.PSObject.Properties['contentManagerAiPath']
        if ($cmProp -and $cmProp.Value) {
            $contentManagerAi = Get-NormalizedPath $cmProp.Value
        }
    }
    if (-not $contentManagerAi) {
        $contentManagerAi = Get-NormalizedPath (Join-Path $repo 'ContentManager-AI')
    }

    if (-not $steam) { throw 'steamInstallPath introuvable (config ou détection auto).' }
    if (-not $ai) { throw 'aiInstallPath introuvable (config ou défaut AssettoCorsa-AI\assettocorsa).' }
    if (-not $docs) { throw 'acDocumentsPath introuvable (config ou détection auto).' }

  return [ordered]@{
        RepoRoot              = $repo
        SteamInstallPath      = $steam
        AiInstallPath         = $ai
        AcDocumentsPath       = $docs
        ContentManagerAiPath  = $contentManagerAi
        ConfigPath            = $ConfigPath
    }
}
