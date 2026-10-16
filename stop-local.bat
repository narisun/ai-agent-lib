@echo off
setlocal
rem Use this checkout's launcher records, regardless of the current folder.
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0tools\stop_local.py" %*
) else (
    python "%~dp0tools\stop_local.py" %*
)
exit /b %errorlevel%
