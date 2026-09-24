@echo off
title VisionSync Camera Configuration (DEBUG MODE)
echo Launching VisionSync Camera Configuration Service in DEBUG Mode...

:: Change directory to the folder where this batch file is located
cd /d "%~dp0"

:: Launch the camera configuration service in a NEW visible CMD window
echo Starting Camera Service (camera_service.py)...
start "VisionSync Camera Service (Debug)" python camera_service.py

:: Wait 4 seconds, then open the config dashboard
echo Opening Config Dashboard...
timeout /t 4 /nobreak > NUL
:: Open it through the service itself (not the file on disk) — the page calls
:: its API at relative /api URLs, which only resolve when served over http.
start http://127.0.0.1:5010/

exit
