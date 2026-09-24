@echo off
title VisionSync Camera Configuration
echo Launching VisionSync Camera Configuration Service...

:: Change directory to the folder where this batch file is located
cd /d "%~dp0"

:: Run pythonw so it is completely invisible
start "" pythonw camera_service.py

:: Wait 4 seconds, then open the config dashboard
timeout /t 4 /nobreak > NUL
:: Open it through the service itself (not the file on disk) — the page calls
:: its API at relative /api URLs, which only resolve when served over http.
start http://127.0.0.1:5010/
exit
