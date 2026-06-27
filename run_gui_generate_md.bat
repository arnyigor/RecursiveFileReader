@echo off
rem ---------------------------------------------
rem  run_gui_generate_md.bat – запускает GUI‑генератор Markdown через .venv
rem ---------------------------------------------

chcp 65001 >nul
setlocal

set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"

pushd "%ROOT%"

if not exist "%VENV_PY%" (
    echo Creating virtual environment: %VENV%
    py -3 -m venv "%VENV%" 2>nul || python -m venv "%VENV%"
)

if not exist "%VENV_PY%" (
    echo ERROR: Failed to create or find .venv Python.
    popd
    pause
    exit /b 1
)

"%VENV_PY%" -c "import dotenv" >nul 2>nul
if errorlevel 1 (
    echo Installing dependency: python-dotenv
    "%VENV_PY%" -m pip install python-dotenv
)

"%VENV_PY%" gui_generate_md.py %*

popd
pause
