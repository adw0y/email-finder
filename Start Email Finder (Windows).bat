@echo off
title Email Finder
cd /d "%~dp0"

rem Find Python (the "py" launcher or "python")
set PY=
where py >nul 2>nul && set PY=py
if not defined PY (
  where python >nul 2>nul && set PY=python
)
if not defined PY (
  echo.
  echo Python isn't installed. Get it from https://www.python.org/downloads/
  echo During setup, tick "Add python.exe to PATH", then run this file again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\streamlit.exe" (
  echo First run: setting things up. This takes a couple of minutes...
  %PY% -m venv .venv
  if errorlevel 1 goto failed
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
  if errorlevel 1 goto failed
)

echo Starting Email Finder. Your browser will open shortly.
echo Keep this window open while you use the app. Close it to stop.
".venv\Scripts\streamlit.exe" run app.py
goto end

:failed
echo.
echo Setup failed. Copy the messages above and send them over.
pause
exit /b 1

:end
pause
