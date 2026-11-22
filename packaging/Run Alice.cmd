@echo off
cd /d "%~dp0"
alice.exe init
if errorlevel 1 goto :error
alice.exe run %*
echo.
pause
exit /b 0

:error
echo.
echo Alice failed to start. See the messages above.
pause
exit /b 1
