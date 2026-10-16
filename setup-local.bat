@echo off
setlocal
rem Use the enterprise-provided Python and its existing pip configuration.
rem Installs runtime packages. Add --dev for the CLI and development tools.
python "%~dp0tools\local_dev.py" setup %*
exit /b %errorlevel%
