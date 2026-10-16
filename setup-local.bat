@echo off
setlocal
rem Use the enterprise-provided Python and its existing pip configuration.
python "%~dp0tools\local_dev.py" setup %*
exit /b %errorlevel%
