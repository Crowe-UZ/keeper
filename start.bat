@echo off
rem Only ASCII in this file: cmd misreads Cyrillic in .bat files.
title Kachalka video
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo First run: creating Python environment...
  py -3.12 -m venv .venv
)
echo Updating yt-dlp...
.venv\Scripts\python.exe -m pip install -U -q "yt-dlp[default]"
.venv\Scripts\python.exe server.py
pause
