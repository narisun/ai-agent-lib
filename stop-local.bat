@echo off
setlocal
rem Use this checkout's launcher records, regardless of the current folder.
python "%~dp0tools\stop_local.py" %*
exit /b %errorlevel%
