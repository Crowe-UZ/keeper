@echo off
rem Only ASCII in this file: cmd misreads Cyrillic in .bat files.
rem Spare way to run the downloader by hand, with a console window. Normally the extension starts it itself.
title Kachalka video
cd /d "%~dp0"
rem No environment yet - install.bat builds it (it finds any Python 3.10+, not only 3.12, and checks every step).
if not exist .venv\Scripts\python.exe (
  echo Not installed yet. Run install.bat first.
  pause
  exit /b 1
)
.venv\Scripts\python.exe server.py
pause
