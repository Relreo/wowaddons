@echo off

rem ── Optional settings ────────────────────────────────────────────────────────
rem
rem  ICON     Path to an .ico file, or "C:\path\to\file.exe,0" to use an exe icon.
rem           Leave blank for no icon.
rem  POSITION Where to appear in the context menu: Top, Bottom, or Default.
rem
set ICON=
set POSITION=Top
rem ─────────────────────────────────────────────────────────────────────────────

set ARGS=-Position %POSITION%
if not "%ICON%"=="" set ARGS=%ARGS% -Icon "%ICON%"

powershell.exe -ExecutionPolicy Bypass -File "%~dp0scripts\install-windows-context-menu.ps1" %ARGS%
pause
