#Requires -Version 5.1
<#
.SYNOPSIS
    Removes the Windows Explorer folder context-menu item added by install-windows-context-menu.ps1.

.DESCRIPTION
    Deletes the HKCU registry keys for the "Update WoW Addons" folder context menu entry.
    No admin rights required.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$verbKey = "Registry::HKEY_CURRENT_USER\Software\Classes\Directory\shell\UpdateWoWAddons"

if (Test-Path -LiteralPath $verbKey) {
    Remove-Item -LiteralPath $verbKey -Recurse -Force
    Write-Host "Removed folder context menu entry (Update WoW Addons)."
}
else {
    Write-Host "Context menu entry was not installed; nothing to remove."
}

