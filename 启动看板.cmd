@echo off
powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0launch_dashboard.ps1" -Open
exit /b %errorlevel%
