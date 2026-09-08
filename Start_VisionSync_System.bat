@echo off
title VisionSync Camera Configuration
echo Launching VisionSync Camera Configuration Service...

:: Change directory to the folder where this batch file is located
cd /d "%~dp0"

:: Run pythonw so it is completely invisible
start "" pythonw camera_service.py

:: Wait 2 seconds, then open the config dashboard
timeout /t 2 /nobreak > NUL
start index.html
exit
