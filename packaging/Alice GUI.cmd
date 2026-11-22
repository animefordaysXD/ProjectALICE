@echo off
cd /d "%~dp0"
if exist "alice-gui.exe" (
  start "" "alice-gui.exe"
) else (
  alice.exe gui
)
