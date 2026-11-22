@echo off
cd /d "%~dp0"
alice.exe report --write
alice.exe report --days 14
pause
