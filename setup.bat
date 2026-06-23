@echo off
title 4K Audio Recorder - Setup
color 0B
echo.
echo  ============================================
echo   4K Audio Recorder Pro - First-Time Setup
echo  ============================================
echo.
echo  Installing required packages...
echo.
pip install sounddevice soundfile numpy
echo.
if %ERRORLEVEL% EQU 0 (
    color 0A
    echo  [OK] Setup complete! Run launch.bat to start the app.
) else (
    color 0C
    echo  [ERROR] Installation failed. Make sure Python is installed.
    echo  Download Python from https://python.org
)
echo.
pause
