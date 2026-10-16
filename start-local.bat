@echo off
setlocal
rem Resolve paths from this file so the command also works from another folder.
python "%~dp0tools\local_dev.py" %*
exit /b %errorlevel%
