@echo off
rem The PowerShell entry point installs uv/Python, syncs uv.lock, and starts Potbot.
rem Example: start.bat service install --workspace-id UUID --principal-id UUID --provider-policy providers.json
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
exit /b %ERRORLEVEL%
