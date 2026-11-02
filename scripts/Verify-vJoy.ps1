#Requires -Version 5.1
<#
.SYNOPSIS
    Verifie l'etat vJoy et l'environnement Windows (Phase C1B3). N'installe rien.
#>
[CmdletBinding()]
param(
    [switch]$CheckSignature
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Section([string]$Title) {
    Write-Host ''
    Write-Host "=== $Title ==="
}

Write-Section 'Windows'
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
$ver = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
Write-Host "OS        : $($os.Caption)"
Write-Host "Version   : $($os.Version)"
Write-Host "Build     : $($os.BuildNumber) (Display $($ver.DisplayVersion), UBR $($ver.UBR))"
Write-Host "Arch      : $($cs.SystemType)"

Write-Section 'vJoy installe'
$paths = @{
    'vJoyInterface x64' = 'C:\Program Files\vJoy\x64\vJoyInterface.dll'
    'vJoyInterface x86' = 'C:\Program Files\vJoy\x86\vJoyInterface.dll'
    'vjoy.sys'          = 'C:\Windows\System32\drivers\vjoy.sys'
    'Configure vJoy'    = 'C:\Program Files\vJoy\x64\Configure vJoy.exe'
}
$installed = $false
foreach ($entry in $paths.GetEnumerator()) {
    $exists = Test-Path -LiteralPath $entry.Value
    if ($exists) { $installed = $true }
    Write-Host ("{0,-20} : {1}" -f $entry.Key, $(if ($exists) { 'present' } else { 'absent' }))
}

$devices = Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object {
    $_.FriendlyName -match 'vJoy|vjoy' -or $_.InstanceId -match 'VID_1234'
}
if ($devices) {
    Write-Host ''
    Write-Host 'Peripheriques vJoy :'
    $devices | ForEach-Object { Write-Host "  [$($_.Status)] $($_.FriendlyName)" }
} else {
    Write-Host 'Peripheriques vJoy : aucun'
}

if ($CheckSignature -and (Test-Path -LiteralPath $paths['vjoy.sys'])) {
    Write-Section 'Signature pilote vjoy.sys'
    try {
        $sig = Get-AuthenticodeSignature -LiteralPath $paths['vjoy.sys']
        Write-Host "Status  : $($sig.Status)"
        Write-Host "Signer  : $($sig.SignerCertificate.Subject)"
    } catch {
        Write-Host "Signature : lecture impossible - $($_.Exception.Message)"
    }
}

Write-Section 'Version recommandee (non installee automatiquement)'
Write-Host 'vJoy 2.2.2.0 - BrunnerInnovation/vJoy (GitHub Releases)'
Write-Host 'URL       : https://github.com/BrunnerInnovation/vJoy/releases/tag/v2.2.2.0'
Write-Host 'Fichier   : vJoySetup_v2.2.2.0_Win10_Win11.exe'
Write-Host 'Raison    : pilote signe EV + attestation Microsoft, Win10/11 x64, HVCI/Secure Boot'
Write-Host 'Eviter    : v2.1.8 SourceForge, v2.2.1.1 non signe, miroirs tiers non officiels'

Write-Section 'Avant installation (action manuelle)'
@(
    '1. Creer un point de restauration Windows.'
    '   Panneau : Systeme - Protection du systeme - Creer'
    '   OU PowerShell admin : Checkpoint-Computer -Description Pre-vJoy-C1B3 -RestorePointType MODIFY_SETTINGS'
    '2. Telecharger uniquement depuis GitHub Releases.'
    '3. Apres install : .\scripts\Verify-vJoy.ps1 -CheckSignature'
) | ForEach-Object { Write-Host $_ }

Write-Section 'Desinstallation / retour arriere'
@(
    '1. Programmes - Desinstaller vJoy.'
    '2. Gestionnaire de peripheriques - vJoy Device - Desinstaller le pilote.'
    '3. Verifier absence de C:\Windows\System32\drivers\vjoy.sys'
    '4. Restauration systeme vers Pre-vJoy-C1B3 si necessaire.'
    '5. Relancer Verify-vJoy.ps1 pour confirmer.'
) | ForEach-Object { Write-Host $_ }

Write-Section 'Resultat'
if ($installed) {
    Write-Host 'vJoy DETECTE - verifier signature et version avant usage.'
    exit 0
}
Write-Host 'vJoy NON INSTALLE - pret pour Phase C1B3 (apres point de restauration).'
exit 0
