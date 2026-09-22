@echo off
setlocal

set "VIEWER_DIR=%~dp0"
for %%I in ("%VIEWER_DIR%..") do set "REPOSITORY_ROOT=%%~fI"
set "VIEWER_PYTHON="

set "RUNTIME_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%RUNTIME_PYTHON%" set "VIEWER_PYTHON=%RUNTIME_PYTHON%"

if not defined VIEWER_PYTHON (
  for /f "delims=" %%I in ('where python 2^>nul') do if not defined VIEWER_PYTHON set "VIEWER_PYTHON=%%I"
)

if not defined VIEWER_PYTHON (
  echo Python was not found. The viewer cannot start.
  echo Start it from Codex or install Python 3 first.
  pause
  exit /b 1
)

set "VIEWER_PORT=%~1"
if not defined VIEWER_PORT set "VIEWER_PORT=8765"

echo Starting the Slay the Spire run viewer...
echo Open http://127.0.0.1:%VIEWER_PORT% after startup.
echo Press Ctrl+C to stop the viewer.
echo.

"%VIEWER_PYTHON%" "%VIEWER_DIR%server.py" --root "%REPOSITORY_ROOT%" --host 127.0.0.1 --port "%VIEWER_PORT%"
set "VIEWER_EXIT=%ERRORLEVEL%"

if not "%VIEWER_EXIT%"=="0" (
  echo.
  echo The viewer stopped with error code %VIEWER_EXIT%.
  pause
)

exit /b %VIEWER_EXIT%
