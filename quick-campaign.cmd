@echo off
setlocal
set "STS_QUICK_PYTHON=%~dp0runtime\python.exe"
if not exist "%STS_QUICK_PYTHON%" set "STS_QUICK_PYTHON=%~dp0.java-python-runtime\python.exe"
if not exist "%STS_QUICK_PYTHON%" set "STS_QUICK_PYTHON=python"
"%STS_QUICK_PYTHON%" -c "pass" >nul 2>&1
if errorlevel 1 (
  echo Python runtime unavailable. Use the complete Windows EXE release.
  exit /b 1
)
"%STS_QUICK_PYTHON%" "%~dp0quick_campaign.py" %*
