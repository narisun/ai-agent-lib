@echo off
setlocal
rem Resolve paths from this file so the command also works from another folder.
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0tools\local_dev.py" %*
) else (
    python "%~dp0tools\local_dev.py" %*
)
exit /b %errorlevel%
