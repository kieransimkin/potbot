@echo off
rem The PowerShell entry point installs uv/Python, syncs uv.lock, and starts Potbot.
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
exit /b %ERRORLEVEL%
