@echo off
if not exist "%LOCALAPPDATA%\Alice" mkdir "%LOCALAPPDATA%\Alice"
start "" explorer "%LOCALAPPDATA%\Alice"
