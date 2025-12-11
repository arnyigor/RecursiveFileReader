@echo off
rem ---------------------------------------------
rem  run_gui_generate_md.bat – запускает GUI‑генератор Markdown
rem ---------------------------------------------

chcp 65001 >nul

pushd "%~dp0"

"%~dp0.venv\Scripts\python.exe" gui_generate_md.py %*

popd
pause
