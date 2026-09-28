<#
.SYNOPSIS
    Installs (or uninstalls) Movement Overhaul for Borderlands 2.

.DESCRIPTION
    Finds Borderlands 2 (Steam, any Steam library, or Epic), checks the
    Willow2 Mod Manager is installed, and copies the mod into sdk_mods.
    An older copy is moved to sdk_mods\.movement_overhaul_backup first, and
    your settings file is never overwritten.

    Easiest: double-click install.bat (or uninstall.bat).

.PARAMETER GamePath
    The Borderlands 2 folder, if it can't be found automatically.

.PARAMETER Uninstall
    Remove the mod instead. Your settings file is left in place.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -GamePath "D:\Games\Borderlands 2"
    .\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$GamePath,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$ModName = 'movement_overhaul'
$SdkUrl = 'https://github.com/bl-sdk/willow2-mod-manager/releases/latest'
$Source = Join-Path $PSScriptRoot "src\$ModName"

function Say([string]$Text, [string]$Color = 'Gray') { Write-Host $Text -ForegroundColor $Color }
function Fail([string]$Text) { Say ''; Say $Text 'Red'; exit 1 }

function Test-GameFolder([string]$Path) {
    return $Path -and (Test-Path -LiteralPath (Join-Path $Path 'Binaries\Win32\Borderlands2.exe'))
}

function Find-SteamGame {
    $roots = @()
    foreach ($key in 'HKCU:\Software\Valve\Steam', 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam', 'HKLM:\SOFTWARE\Valve\Steam') {
        try {
            $item = Get-ItemProperty -Path $key -ErrorAction Stop
            foreach ($name in 'SteamPath', 'InstallPath') {
                if ($item.$name) { $roots += ($item.$name -replace '/', '\') }
            }
        } catch { }
    }
    $libraries = @()
    foreach ($root in ($roots | Select-Object -Unique)) {
        $libraries += $root
        $vdf = Join-Path $root 'steamapps\libraryfolders.vdf'
        if (Test-Path -LiteralPath $vdf) {
            foreach ($match in [regex]::Matches((Get-Content -LiteralPath $vdf -Raw), '"path"\s+"([^"]+)"')) {
                $libraries += ($match.Groups[1].Value -replace '\\\\', '\')
            }
        }
    }
    foreach ($library in ($libraries | Select-Object -Unique)) {
        $candidate = Join-Path $library 'steamapps\common\Borderlands 2'
        if (Test-GameFolder $candidate) { return $candidate }
    }
    return $null
}

function Find-EpicGame {
    $manifests = Join-Path $env:ProgramData 'Epic\EpicGamesLauncher\Data\Manifests'
    if (-not (Test-Path -LiteralPath $manifests)) { return $null }
    foreach ($file in Get-ChildItem -LiteralPath $manifests -Filter '*.item' -ErrorAction SilentlyContinue) {
        try {
            $manifest = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
            if (Test-GameFolder $manifest.InstallLocation) { return $manifest.InstallLocation }
        } catch { }
    }
    return $null
}

Say ''
Say 'Movement Overhaul for Borderlands 2' 'Cyan'
Say '-----------------------------------' 'Cyan'

# --- find the game -------------------------------------------------------------
if (-not $GamePath) { $GamePath = Find-SteamGame }
if (-not $GamePath) { $GamePath = Find-EpicGame }
if (-not $GamePath) {
    Say "Couldn't find Borderlands 2 automatically." 'Yellow'
    $GamePath = (Read-Host 'Paste your Borderlands 2 folder (the one containing Binaries)').Trim('"', ' ')
}
if (-not (Test-GameFolder $GamePath)) {
    Fail "That doesn't look like a Borderlands 2 folder (no Binaries\Win32\Borderlands2.exe): $GamePath"
}
Say "Game:     $GamePath"

$SdkMods = Join-Path $GamePath 'sdk_mods'
if (-not (Test-Path -LiteralPath $SdkMods)) {
    Fail ("The Willow2 Mod Manager (Python SDK) isn't installed - there's no sdk_mods folder.`n" +
          "Install it first from $SdkUrl, start the game once, then run this again.")
}

if (Get-Process -Name 'Borderlands2' -ErrorAction SilentlyContinue) {
    Fail 'Borderlands 2 is running. Close the game and run this again.'
}

# --- move any existing copy aside --------------------------------------------
$existing = @(
    (Join-Path $SdkMods "$ModName.sdkmod"),
    (Join-Path $SdkMods $ModName)
) | Where-Object { Test-Path -LiteralPath $_ }

if ($existing) {
    $backup = Join-Path $SdkMods (".movement_overhaul_backup\" + (Get-Date -Format 'yyyy-MM-dd_HH-mm-ss'))
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    foreach ($path in $existing) { Move-Item -LiteralPath $path -Destination $backup }
    Say "Backup:   previous version moved to sdk_mods\.movement_overhaul_backup"
}

if ($Uninstall) {
    if (-not $existing) { Say 'Movement Overhaul was not installed.' 'Yellow' }
    else { Say ''; Say 'Uninstalled. Your settings are kept in sdk_mods\settings.' 'Green' }
    exit 0
}

# --- install ---------------------------------------------------------------------
if (-not (Test-Path -LiteralPath (Join-Path $Source '__init__.py'))) {
    Fail "Can't find the mod files next to this script ($Source). Run it from the downloaded folder."
}

if (-not (Test-Path -LiteralPath (Join-Path $SdkMods 'networking.sdkmod')) -and
    -not (Test-Path -LiteralPath (Join-Path $SdkMods 'networking'))) {
    Say "Warning:  the SDK's 'networking' library wasn't found. Update the mod manager if the mod fails to load." 'Yellow'
}

$target = Join-Path $SdkMods $ModName
Copy-Item -LiteralPath $Source -Destination $target -Recurse
Get-ChildItem -LiteralPath $target -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
Say "Install:  sdk_mods\$ModName"

# Turn the mod on for a first install; an existing settings file is left alone.
$settingsDir = Join-Path $SdkMods 'settings'
$settings = Join-Path $settingsDir "$ModName.json"
if (-not (Test-Path -LiteralPath $settings)) {
    New-Item -ItemType Directory -Path $settingsDir -Force | Out-Null
    [IO.File]::WriteAllText($settings, "{`n    `"enabled`": true`n}`n")
    Say 'Settings: enabled on first launch'
} else {
    Say 'Settings: kept your existing settings'
}

# The standalone Sliding mod does the same job and doubles every slide.
$conflicts = 'sliding.sdkmod', 'sliding' | ForEach-Object { Join-Path $SdkMods $_ } | Where-Object { Test-Path -LiteralPath $_ }
if ($conflicts) {
    Say ''
    Say "Note: you also have juso's Sliding mod installed. Movement Overhaul includes it -" 'Yellow'
    Say '      turn Sliding off in the Mods menu, or every slide happens twice.' 'Yellow'
}

Say ''
Say 'Done! Start the game - Movement Overhaul is under Mods.' 'Green'
Say 'Dash is on Left Shift by default: Mods > Movement Overhaul > Keybinds to change it.'
Say 'Left Shift is also the game''s default Sprint key - rebind Sprint in the game''s options if they clash.'
