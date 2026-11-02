#Requires -Version 5.1
<#
.SYNOPSIS
    Alias de compatibilite : delegue vers Start-ContentManager-AI.ps1.
#>
[CmdletBinding()]
param(
    [switch]$SkipProfileSwitch,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $scriptDir 'Start-ContentManager-AI.ps1') @PSBoundParameters
