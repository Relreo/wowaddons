#Requires -Version 5.1
<#
.SYNOPSIS
    Adds a Windows Explorer folder context-menu item that runs wowaddons update.

.DESCRIPTION
    Writes to HKCU (no admin). The entry appears under the classic menu:
    right-click a folder → Show more options → "Update WoW Addons".

    wowaddons scans the current working directory, so the command must start
    a shell with that directory set to the folder you right-clicked.

    The icon is auto-detected: WoW.exe (via Blizzard registry) → python.exe → none.
    Pass -Icon to override.

.PARAMETER WowAddonsLauncher
    Command used to invoke wowaddons (must be on PATH for Explorer-spawned shells,
    unless you pass a full path). Examples: wowaddons, py -m wowaddons

.PARAMETER UseLegacyCmd
    Use cmd.exe with "cd /d" instead of Windows Terminal (wt.exe). Use if wt.exe
    is not installed.

.PARAMETER CloseOnExit
    Use cmd /c instead of cmd /k so the window closes when the command finishes.

.PARAMETER Icon
    Override the auto-detected icon. Path to an .ico file or "C:\file.exe,0".
    Pass an empty string to force no icon.

.PARAMETER Position
    Where to place the entry in the context menu: Top, Bottom, or Default (let Windows decide).
#>
[CmdletBinding()]
param(
    [string] $WowAddonsLauncher = "wowaddons",
    [switch] $UseLegacyCmd,
    [switch] $CloseOnExit,
    [string] $Icon,
    [ValidateSet("Top", "Bottom", "Default")]
    [string] $Position = "Default"
)

$ErrorActionPreference = "Stop"

# ── Icon auto-detection ───────────────────────────────────────────────────────

function Find-WoWIcon {
    # Collect candidate install roots: default paths first, then Blizzard registry
    $installRoots = [System.Collections.Generic.List[string]]::new()

    foreach ($base in @("${Env:ProgramFiles(x86)}\World of Warcraft", "$Env:ProgramFiles\World of Warcraft")) {
        if ($base) { $installRoots.Add($base) }
    }

    foreach ($reg in @(
        "HKLM:\SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft",
        "HKLM:\SOFTWARE\Blizzard Entertainment\World of Warcraft"
    )) {
        try {
            $p = (Get-ItemProperty -LiteralPath $reg -ErrorAction Stop).InstallPath
            if ($p) { $installRoots.Add($p) }
        }
        catch {}
    }

    foreach ($root in $installRoots) {
        foreach ($rel in @("_retail_\Wow.exe", "_classic_\WowClassic.exe", "Wow.exe")) {
            $exe = Join-Path $root $rel
            if (Test-Path $exe) { return "$exe,0" }
        }
    }
    return $null
}

function Find-PythonIcon {
    foreach ($cmd in @("python", "py", "python3")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found -and (Test-Path $found.Source)) { return "$($found.Source),0" }
    }
    return $null
}

# Only auto-detect when -Icon was not supplied at all (not even as empty string)
$iconSource = $null
if (-not $PSBoundParameters.ContainsKey("Icon")) {
    $Icon = Find-WoWIcon
    if ($Icon) {
        $iconSource = "WoW executable"
    }
    else {
        $Icon = Find-PythonIcon
        if ($Icon) { $iconSource = "Python executable" }
    }
}

# ── Build registry command line ───────────────────────────────────────────────

$inner = "$WowAddonsLauncher update".Trim()
if (-not $inner) {
    Write-Error "WowAddonsLauncher cannot be empty."
    exit 1
}

$windowTitle = "WoW Addon Updater"
$cmdFlag = if ($CloseOnExit) { "/c" } else { "/k" }

if ($UseLegacyCmd) {
    $commandLine = "cmd.exe $cmdFlag title $windowTitle && cd /d `"%1`" && $inner"
}
else {
    $commandLine = "wt.exe --title `"$windowTitle`" -d `"%1`" cmd $cmdFlag $inner"
}

# ── Write registry ────────────────────────────────────────────────────────────

$verbKey    = "Registry::HKEY_CURRENT_USER\Software\Classes\Directory\shell\UpdateWoWAddons"
$commandKey = "$verbKey\command"

New-Item -Path $verbKey -Force | Out-Null
Set-ItemProperty -LiteralPath $verbKey -Name "(default)" -Value "Update WoW Addons"

if ($Icon) {
    Set-ItemProperty -LiteralPath $verbKey -Name "Icon" -Value $Icon
}
else {
    Remove-ItemProperty -LiteralPath $verbKey -Name "Icon" -ErrorAction SilentlyContinue
}

if ($Position -ne "Default") {
    Set-ItemProperty -LiteralPath $verbKey -Name "Position" -Value $Position
}
else {
    Remove-ItemProperty -LiteralPath $verbKey -Name "Position" -ErrorAction SilentlyContinue
}

New-Item -Path $commandKey -Force | Out-Null
Set-ItemProperty -LiteralPath $commandKey -Name "(default)" -Value $commandLine

# ── Summary ───────────────────────────────────────────────────────────────────

Write-Host "Installed folder context menu entry."
Write-Host "  Use: right-click a folder -> Show more options -> Update WoW Addons"
Write-Host "  Command: $commandLine"
if ($Icon) {
    $label = if ($iconSource) { " (auto-detected from $iconSource)" } else { " (manual)" }
    Write-Host "  Icon: $Icon$label"
}
else {
    Write-Host "  Icon: none"
}
