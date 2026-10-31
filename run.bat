@echo off
setlocal
set "PYTHONUTF8=1"
pushd "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" goto venv
where py >nul 2>nul
if not errorlevel 1 goto launcher
where python >nul 2>nul
if not errorlevel 1 goto systempython
echo Python 3.10+ is required. See README.md for setup.
set "ROUTING_EXIT=1"
goto done
:venv
"%~dp0.venv\Scripts\python.exe" -m payment_routing %*
goto result
:launcher
py -3 -m payment_routing %*
goto result
:systempython
python -m payment_routing %*
:result
set "ROUTING_EXIT=%errorlevel%"
:done
popd
exit /b %ROUTING_EXIT%
