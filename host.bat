@echo off
rem Chrome runs this file when the extension asks to start the downloader. Only ASCII here.
"%~dp0.venv\Scripts\python.exe" "%~dp0launcher.py"
