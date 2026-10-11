@echo off
rem Only ASCII in this file: cmd misreads Cyrillic in .bat files.
rem Run once: after that the Chrome extension starts the helper program by itself.
rem Every step is checked: on failure the script stops and says what is wrong instead of printing "Done".
title Keeper - install
cd /d "%~dp0"
setlocal

rem --- 1. Python: 3.12 is preferred, any Python 3.10+ works -------------------------------------------
set "PY="
py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
if not defined PY py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=python"
if not defined PY (
  echo.
  echo ERROR: Python 3.10 or newer was not found.
  echo   Open "Terminal" from the Start menu and run:   winget install Python.Python.3.12
  echo   Then close this window and run install.bat again.
  goto :fail
)
echo Python found: %PY%

rem --- 2. Own environment; rebuilt if it is broken (for example, Python was reinstalled) --------------
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import sys" >nul 2>nul || (
    echo The old environment is broken - creating it again...
    rmdir /s /q .venv
  )
)
if not exist .venv\Scripts\python.exe (
  echo Creating Python environment...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo ERROR: could not create the Python environment in "%~dp0.venv".
    goto :fail
  )
)

rem --- 3. Libraries ---------------------------------------------------------------------------------
echo Installing libraries (needs internet, takes a minute)...
.venv\Scripts\python.exe -m pip install -U -q "yt-dlp[default]" pystray pillow sherpa-onnx==1.13.8
if errorlevel 1 (
  echo ERROR: could not download the libraries. Check the internet connection and run install.bat again.
  goto :fail
)
.venv\Scripts\python.exe -c "import yt_dlp, pystray, PIL, sherpa_onnx"
if errorlevel 1 (
  echo ERROR: the libraries were installed but do not load.
  goto :fail
)

rem --- 4. ffmpeg: joins video with sound, makes audio files, feeds speech recognition -----------------
set "FF=1"
where ffmpeg >nul 2>nul || set "FF="
if not defined FF if exist "%LOCALAPPDATA%\Microsoft\WinGet\Packages" dir /s /b "%LOCALAPPDATA%\Microsoft\WinGet\Packages\ffmpeg.exe" >nul 2>nul && set "FF=1"
if not defined FF (
  echo ffmpeg was not found - installing it with winget...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  if errorlevel 1 (
    echo.
    echo WARNING: ffmpeg could not be installed automatically.
    echo   Without it video quality is lower and sound and text do not work.
    echo   Open "Terminal" from the Start menu and run:   winget install Gyan.FFmpeg
  )
)

rem --- 5. Tell Chrome how to start the helper ---------------------------------------------------------
.venv\Scripts\python.exe launcher.py --install
if errorlevel 1 (
  echo ERROR: could not register the helper for Chrome.
  goto :fail
)

echo.
echo OK - everything is installed.
echo Now open the Keeper window in Chrome (the extension icon). If the extension is not added yet:
echo   chrome://extensions  -  turn on "Developer mode"  -  "Load unpacked"  -  choose the folder "extension".
pause
exit /b 0

:fail
echo.
echo Installation did NOT finish. Fix the problem above and run install.bat again.
pause
exit /b 1
