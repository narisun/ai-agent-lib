@echo off
setlocal
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0tools\phoenix_dev.py" %*
) else (
    python "%~dp0tools\phoenix_dev.py" %*
)
exit /b %errorlevel%
