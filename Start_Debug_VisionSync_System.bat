@echo off
title VisionSync Camera Configuration (DEBUG MODE)
echo Launching VisionSync Camera Configuration Service in DEBUG Mode...

:: Change directory to the folder where this batch file is located
cd /d "%~dp0"

:: Launch the camera configuration service in a NEW visible CMD window
echo Starting Camera Service (camera_service.py)...
start "VisionSync Camera Service (Debug)" python camera_service.py

:: Wait 2 seconds, then open the config dashboard
echo Opening Config Dashboard...
timeout /t 2 /nobreak > NUL
start index.html

exit
