#Requires -Version 5.1
<#
.SYNOPSIS
    Sauvegarde et restauration des fichiers Assetto Corsa avant installation du plug-in AC Gym.

.DESCRIPTION
    Phase B : ne copie rien dans le jeu. Prépare une sauvegarde horodatée hors du dossier AC.
    Ce script ne supprime jamais de fichier (aucun Remove-Item).

.PARAMETER Operation
    Backup : crée une sauvegarde. Restore : restaure depuis -BackupPath (obligatoire). Verify : lecture seule.

.PARAMETER DryRun
    Affiche les actions sans créer de dossier ni copier de fichier.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [Alias('Action')]
    [ValidateSet('Backup', 'Restore', 'Verify')]
    [string]$Operation = 'Backup',

    [string]$AcInstallPath,
    [string]$AcDocumentsPath,
    [string]$BackupPath,
    [string]$BackupRoot,
    [string]$RepoRoot,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Info([string]$Message) { Write-Host "[INFO] $Message" }
function Write-Warn([string]$Message) { Write-Host "[WARN] $Message" -ForegroundColor Yellow }

function Get-ProjectRoot {
    param([string]$StartPath = $PSScriptRoot)
    return (Resolve-Path -LiteralPath (Join-Path $StartPath '..')).Path
}

function Get-SteamPath {
    $reg = Get-ItemProperty -Path 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue
    if ($reg -and $reg.SteamPath -and (Test-Path -LiteralPath $reg.SteamPath)) {
        return $reg.SteamPath
    }
    return $null
}

function Get-SteamLibraryPaths {
    param([string]$SteamPath)
    $libraries = @($SteamPath)
    $vdf = Join-Path $SteamPath 'steamapps\libraryfolders.vdf'
    if (-not (Test-Path -LiteralPath $vdf)) { return $libraries }

    $content = Get-Content -LiteralPath $vdf -Raw
    $rxMatches = [regex]::Matches($content, '"path"\s+"([^"]+)"')
    foreach ($m in $rxMatches) {
        $p = $m.Groups[1].Value -replace '\\\\', '\'
        if (Test-Path -LiteralPath $p) { $libraries += $p }
    }
    return $libraries | Select-Object -Unique
}

function Find-AssettoCorsaInstallPath {
    param([string]$PreferredPath)

    if ($PreferredPath) {
        $exe = Join-Path $PreferredPath 'AssettoCorsa.exe'
        if (Test-Path -LiteralPath $exe) { return (Resolve-Path -LiteralPath $PreferredPath).Path }
        throw "AcInstallPath invalide : AssettoCorsa.exe introuvable dans '$PreferredPath'"
    }

    $steam = Get-SteamPath
    if ($steam) {
        foreach ($lib in (Get-SteamLibraryPaths -SteamPath $steam)) {
            $candidate = Join-Path $lib 'steamapps\common\assettocorsa'
            $exe = Join-Path $candidate 'AssettoCorsa.exe'
            if (Test-Path -LiteralPath $exe) {
                return (Resolve-Path -LiteralPath $candidate).Path
            }
        }
    }

    $fallbacks = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Steam\steamapps\common\assettocorsa')
        (Join-Path $env:ProgramFiles 'Steam\steamapps\common\assettocorsa')
    )
    foreach ($candidate in $fallbacks) {
        if (Test-Path -LiteralPath (Join-Path $candidate 'AssettoCorsa.exe')) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw @"
Assetto Corsa introuvable automatiquement.
Relancez avec : -AcInstallPath 'CHEMIN\steamapps\common\assettocorsa'
"@
}

function Find-AssettoCorsaDocumentsPath {
    param([string]$PreferredPath)

    if ($PreferredPath) {
        if (Test-Path -LiteralPath $PreferredPath) {
            return (Resolve-Path -LiteralPath $PreferredPath).Path
        }
        throw "AcDocumentsPath invalide : '$PreferredPath' introuvable"
    }

    $candidates = @(
        (Join-Path $env:USERPROFILE 'OneDrive\Documents\Assetto Corsa')
        (Join-Path $env:USERPROFILE 'Documents\Assetto Corsa')
        (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Assetto Corsa')
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) {
            return (Resolve-Path -LiteralPath $c).Path
        }
    }

    throw @"
Dossier Documents Assetto Corsa introuvable automatiquement.
Relancez avec : -AcDocumentsPath 'CHEMIN\Documents\Assetto Corsa'
"@
}

function Get-WindowsLibsRoot {
    param([string]$RepoRootPath)
    $path = Join-Path $RepoRootPath 'assetto_corsa_gym\AssettoCorsaPlugin\windows-libs'
    if (-not (Test-Path -LiteralPath $path)) {
        throw "windows-libs introuvable : $path"
    }
    return (Resolve-Path -LiteralPath $path).Path
}

function Get-InstallationTargets {
    param(
        [string]$AcInstall,
        [string]$AcDocs,
        [string]$WindowsLibsRoot
    )

    $targets = [ordered]@{
        GamePluginFolders  = @(
            (Join-Path $AcInstall 'apps\python\sensor_par')
            (Join-Path $AcInstall 'apps\python\sensors_par')
        )
        DocumentFiles      = @(
            (Join-Path $AcDocs 'cfg\controllers\savedsetups\Vjoy.ini')
            (Join-Path $AcDocs 'cfg\controllers\savedsetups\WASD.ini')
            (Join-Path $AcDocs 'cfg\python.ini')
        )
        WindowsLibDllFiles = @()
        WindowsLibLibFiles = @()
    }

    $dllSource = Join-Path $WindowsLibsRoot 'DLLs'
    if (Test-Path -LiteralPath $dllSource) {
        Get-ChildItem -LiteralPath $dllSource -Recurse -File | ForEach-Object {
            $rel = $_.FullName.Substring($dllSource.Length).TrimStart('\')
            $targets.WindowsLibDllFiles += Join-Path $AcInstall (Join-Path 'system\x64\DLLs' $rel)
        }
    }

    $libSource = Join-Path $WindowsLibsRoot 'Lib'
    if (Test-Path -LiteralPath $libSource) {
        Get-ChildItem -LiteralPath $libSource -Recurse -File | ForEach-Object {
            $rel = $_.FullName.Substring($libSource.Length).TrimStart('\')
            $targets.WindowsLibLibFiles += Join-Path $AcInstall (Join-Path 'system\x64\Lib' $rel)
        }
    }

    return $targets
}

function New-FileEntry {
    param(
        [string]$Scope,
        [string]$SourcePath,
        [string]$RootPath,
        [string]$BackupPrefix
    )

    $rel = $SourcePath.Substring($RootPath.Length).TrimStart('\')
    $relNormalized = $rel -replace '/', [System.IO.Path]::DirectorySeparatorChar
    return [ordered]@{
        Scope             = $Scope
        SourcePath        = $SourcePath
        RelativePath      = ($rel -replace '\\', '/')
        BackupPath        = (Join-Path $BackupPrefix $relNormalized)
        SizeBytes         = $null
        LastWriteTimeUtc  = $null
        Sha256            = $null
    }
}

function New-BackupManifest {
    param(
        [string]$AcInstall,
        [string]$AcDocs,
        [string]$WindowsLibsRoot,
        [hashtable]$Targets
    )

    $entries = [System.Collections.Generic.List[object]]::new()

    foreach ($folder in $Targets.GamePluginFolders) {
        if (Test-Path -LiteralPath $folder) {
            Get-ChildItem -LiteralPath $folder -Recurse -File | ForEach-Object {
                $entries.Add((New-FileEntry -Scope 'game' -SourcePath $_.FullName -RootPath $AcInstall -BackupPrefix 'game'))
            }
        }
    }

    foreach ($file in $Targets.DocumentFiles) {
        if (Test-Path -LiteralPath $file) {
            $entries.Add((New-FileEntry -Scope 'documents' -SourcePath $file -RootPath $AcDocs -BackupPrefix 'documents'))
        }
    }

    foreach ($file in ($Targets.WindowsLibDllFiles + $Targets.WindowsLibLibFiles)) {
        if (Test-Path -LiteralPath $file) {
            $entries.Add((New-FileEntry -Scope 'game' -SourcePath $file -RootPath $AcInstall -BackupPrefix 'game'))
        }
    }

    return [ordered]@{
        SchemaVersion     = 1
        CreatedAtUtc      = (Get-Date).ToUniversalTime().ToString('o')
        AcInstallPath     = $AcInstall
        AcDocumentsPath   = $AcDocs
        WindowsLibsRoot   = $WindowsLibsRoot
        EntryCount        = $entries.Count
        Entries           = $entries
    }
}

    param(
        [string]$AcInstall,
        [string]$AcDocs,
        [string]$WindowsLibsRoot,
        [string]$DestBackupRoot,
        [switch]$IsDryRun
    )

    $timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $backupDir = Join-Path $DestBackupRoot $timestamp
    $targets = Get-InstallationTargets -AcInstall $AcInstall -AcDocs $AcDocs -WindowsLibsRoot $WindowsLibsRoot
    $manifest = New-BackupManifest -AcInstall $AcInstall -AcDocs $AcDocs -WindowsLibsRoot $WindowsLibsRoot -Targets $targets

    Write-Info "Installation AC : $AcInstall"
    Write-Info "Documents AC    : $AcDocs"
    Write-Info "Sauvegarde      : $backupDir"
    Write-Info "Entrées à sauvegarder : $($manifest.EntryCount)"

    if ($manifest.EntryCount -eq 0) {
        Write-Warn "Aucun fichier existant à sauvegarder (cibles absentes avant installation)."
    }

    if ($IsDryRun) {
        Write-Info "[DRY-RUN] Aucun dossier créé, aucun fichier copié."
        foreach ($e in $manifest.Entries) {
            Write-Host "  $($e.SourcePath) -> $(Join-Path $backupDir $e.BackupPath)"
        }
        return $backupDir
    }

    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    $enriched = [System.Collections.Generic.List[object]]::new()

    foreach ($entry in $manifest.Entries) {
        $dest = Join-Path $backupDir $entry.BackupPath
        $destParent = [System.IO.Path]::GetDirectoryName($dest)
        if (-not (Test-Path -LiteralPath $destParent)) {
            try {
                New-Item -ItemType Directory -Path $destParent -Force | Out-Null
            } catch {
                throw "Echec creation dossier '$destParent' : $_"
            }
        }
        try {
            Copy-Item -LiteralPath $entry.SourcePath -Destination $dest -Force
        } catch {
            throw "Echec copie vers '$dest' : $_"
        }
        $entryHash = [ordered]@{
            Scope            = $entry.Scope
            SourcePath       = $entry.SourcePath
            RelativePath     = $entry.RelativePath
            BackupPath       = $entry.BackupPath
            SizeBytes        = $null
            LastWriteTimeUtc = $null
            Sha256           = $null
        }
        try {
            $item = Get-Item -LiteralPath $entry.SourcePath
            $entryHash['SizeBytes'] = $item.Length
            $entryHash['LastWriteTimeUtc'] = $item.LastWriteTimeUtc.ToString('o')
            $entryHash['Sha256'] = (Get-FileHash -LiteralPath $entry.SourcePath -Algorithm SHA256).Hash
        } catch {
            throw "Echec metadata pour '$($entry.SourcePath)' : $_"
        }
        $enriched.Add($entryHash)
    }

    $manifest.Entries = $enriched
    $manifest.EntryCount = $enriched.Count
    $manifestPath = Join-Path $backupDir 'manifest.json'
    $manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    Write-Info "Manifeste écrit : $manifestPath"
    return $backupDir
}

function Get-ManifestEntries {
    param($ManifestObject)

    if ($null -eq $ManifestObject.Entries) { return @() }
    if ($ManifestObject.Entries -is [System.Array]) { return $ManifestObject.Entries }
    return @($ManifestObject.Entries)
}

function Test-BackupPathSafe {
    param(
        [string]$BackupDir,
        [string]$ExpectedBackupRoot
    )

    if (-not $BackupPath) {
        throw "Restore requiert -BackupPath explicite vers un dossier horodaté (ex. ...\ac_backups\YYYYMMDD_HHMMSS)."
    }

    $resolved = (Resolve-Path -LiteralPath $BackupDir).Path
    $resolvedRoot = if (Test-Path -LiteralPath $ExpectedBackupRoot) {
        (Resolve-Path -LiteralPath $ExpectedBackupRoot).Path
    } else {
        $ExpectedBackupRoot
    }

    if ($resolved -eq $resolvedRoot) {
        throw "BackupPath ne peut pas être la racine ac_backups ; précisez un sous-dossier horodaté."
    }

    $leaf = Split-Path -Leaf $resolved
    if ($leaf -notmatch '^\d{8}_\d{6}$') {
        throw "BackupPath doit pointer vers un dossier horodaté YYYYMMDD_HHMMSS : '$resolved'"
    }

    if ($resolvedRoot -and ($resolved -notlike "$resolvedRoot*")) {
        throw "BackupPath hors de BackupRoot attendu : $resolved"
    }

    return $resolved
}

function Invoke-RestoreAction {
    param(
        [string]$BackupDir,
        [string]$AcInstall,
        [string]$AcDocs,
        [switch]$IsDryRun
    )

    $manifestPath = Join-Path $BackupDir 'manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        throw "Manifeste introuvable : $manifestPath"
    }

    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $entries = Get-ManifestEntries -ManifestObject $manifest

    if ($manifest.AcInstallPath -and ($manifest.AcInstallPath -ne $AcInstall)) {
        throw "Refus : AcInstallPath actuel '$AcInstall' ≠ manifeste '$($manifest.AcInstallPath)'"
    }
    if ($manifest.AcDocumentsPath -and ($manifest.AcDocumentsPath -ne $AcDocs)) {
        throw "Refus : AcDocumentsPath actuel '$AcDocs' ≠ manifeste '$($manifest.AcDocumentsPath)'"
    }

    Write-Info "Restauration depuis : $BackupDir ($($entries.Count) entrées)"

    foreach ($e in $entries) {
        $src = Join-Path $BackupDir ($e.BackupPath -replace '/', '\')
        if (-not (Test-Path -LiteralPath $src)) {
            Write-Warn "Fichier de sauvegarde manquant : $src"
            continue
        }

        $dest = if ($e.Scope -eq 'documents') {
            Join-Path $AcDocs ($e.RelativePath -replace '/', '\')
        } else {
            Join-Path $AcInstall ($e.RelativePath -replace '/', '\')
        }

        if ($IsDryRun) {
            Write-Host "  [DRY-RUN] $src -> $dest"
            continue
        }

        $destParent = [System.IO.Path]::GetDirectoryName($dest)
        if (-not (Test-Path -LiteralPath $destParent)) {
            New-Item -ItemType Directory -Path $destParent -Force | Out-Null
        }
        Copy-Item -LiteralPath $src -Destination $dest -Force
        Write-Host "  Restauré : $dest"
    }
}

function Invoke-VerifyAction {
    param(
        [string]$AcInstall,
        [string]$AcDocs,
        [string]$WindowsLibsRoot,
        [string]$DestBackupRoot
    )

    Write-Info "Vérification des chemins (lecture seule)."
    Write-Info "AC install  : OK — $AcInstall"
    Write-Info "AC documents: OK — $AcDocs"
    Write-Info "windows-libs: OK — $WindowsLibsRoot"
    Write-Info "Backup root : $(if (Test-Path -LiteralPath $DestBackupRoot) { 'OK' } else { 'sera créé' }) — $DestBackupRoot"

    $targets = Get-InstallationTargets -AcInstall $AcInstall -AcDocs $AcDocs -WindowsLibsRoot $WindowsLibsRoot
    $manifest = New-BackupManifest -AcInstall $AcInstall -AcDocs $AcDocs -WindowsLibsRoot $WindowsLibsRoot -Targets $targets

    Write-Info "Fichiers existants à sauvegarder : $($manifest.EntryCount)"
    Write-Info "Référentiel futur DLLs (dépôt) : $($targets.WindowsLibDllFiles.Count)"
    Write-Info "Référentiel futur Lib (dépôt) : $($targets.WindowsLibLibFiles.Count)"

    $existingDll = @($targets.WindowsLibDllFiles | Where-Object { Test-Path -LiteralPath $_ }).Count
    $existingLib = @($targets.WindowsLibLibFiles | Where-Object { Test-Path -LiteralPath $_ }).Count
    Write-Info "DLLs déjà présents dans AC : $existingDll"
    Write-Info "Lib déjà présents dans AC : $existingLib"
}

# --- Main ---
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $scriptDir 'Get-AcLocalPaths.ps1')
$pathCfg = Get-AcLocalPaths -RepoRoot $RepoRoot

$projectRoot = if ($RepoRoot) { (Resolve-Path -LiteralPath $RepoRoot).Path } else { $pathCfg.RepoRoot }
if ($BackupRoot) {
    if (Test-Path -LiteralPath $BackupRoot) {
        $resolvedBackupRoot = (Resolve-Path -LiteralPath $BackupRoot).Path
    } else {
        $resolvedBackupRoot = $BackupRoot
    }
} else {
    $resolvedBackupRoot = Join-Path $projectRoot 'ac_backups'
}

$acInstall = if ($AcInstallPath) { Find-AssettoCorsaInstallPath -PreferredPath $AcInstallPath } else { $pathCfg.SteamInstallPath }
$acDocs = if ($AcDocumentsPath) {
    if (Test-Path -LiteralPath $AcDocumentsPath) { (Resolve-Path -LiteralPath $AcDocumentsPath).Path }
    else { throw "AcDocumentsPath invalide : $AcDocumentsPath" }
} else {
    $pathCfg.AcDocumentsPath
}
$windowsLibs = Get-WindowsLibsRoot -RepoRootPath $projectRoot

switch ($Operation) {
    'Backup' {
        if (-not $DryRun -and -not (Test-Path -LiteralPath $resolvedBackupRoot)) {
            New-Item -ItemType Directory -Path $resolvedBackupRoot -Force | Out-Null
        }
        $result = Invoke-BackupAction -AcInstall $acInstall -AcDocs $acDocs -WindowsLibsRoot $windowsLibs -DestBackupRoot $resolvedBackupRoot -IsDryRun:$DryRun
        Write-Info "Action Backup terminée."
        if ($DryRun) { Write-Info "Dry-run : dossier prévu = $result" }
    }
    'Restore' {
        $safeBackup = Test-BackupPathSafe -BackupDir $BackupPath -ExpectedBackupRoot $resolvedBackupRoot
        Invoke-RestoreAction -BackupDir $safeBackup -AcInstall $acInstall -AcDocs $acDocs -IsDryRun:$DryRun
        Write-Info "Action Restore terminée."
    }
    'Verify' {
        Invoke-VerifyAction -AcInstall $acInstall -AcDocs $acDocs -WindowsLibsRoot $windowsLibs -DestBackupRoot $resolvedBackupRoot
        Write-Info "Action Verify terminée."
    }
}
