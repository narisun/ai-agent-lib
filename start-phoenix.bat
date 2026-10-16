@echo off
setlocal
python "%~dp0tools\phoenix_dev.py" %*
exit /b %errorlevel%
