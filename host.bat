@echo off
rem Chrome runs this file when the extension asks to start the downloader. Only ASCII here.
rem The installer puts its own Python into "python"; a copy from the repository uses ".venv" made by install.bat.
if exist "%~dp0python\python.exe" (
  "%~dp0python\python.exe" "%~dp0launcher.py"
) else (
  "%~dp0.venv\Scripts\python.exe" "%~dp0launcher.py"
)
