@echo off
rem Only ASCII in this file: cmd misreads Cyrillic in .bat files.
rem Run once: after that the Chrome extension starts the downloader by itself.
title Kachalka video - install
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating Python environment...
  py -3.12 -m venv .venv
)
echo Installing libraries...
.venv\Scripts\python.exe -m pip install -U -q "yt-dlp[default]" pystray pillow sherpa-onnx
.venv\Scripts\python.exe launcher.py --install
echo.
echo Done. Now open chrome://extensions and press the reload button on the extension card.
pause
