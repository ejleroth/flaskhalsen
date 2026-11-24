@echo off
title Flaskhalsen
cd /d "%~dp0"
"%~dp0runtime\python.exe" "%~dp0app\run.py" %*
if errorlevel 1 pause
