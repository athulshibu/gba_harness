@echo off
rem Keep the environment active when called from an existing Command Prompt.
pushd "%~dp0"
if errorlevel 1 exit /b 1

if exist "codespace\Scripts\python.exe" goto activate
if exist "codespace" (
    echo ERROR: codespace exists but is not a valid Windows Python virtual environment.
    popd
    exit /b 1
)

where py >nul 2>&1
if errorlevel 1 (
    python -m venv codespace
) else (
    py -3 -m venv codespace
)
if errorlevel 1 goto failed

:activate
call "codespace\Scripts\activate.bat"
if errorlevel 1 goto failed
python -m pip install requests pillow
if errorlevel 1 goto failed

echo.
echo codespace is active and dependencies are installed.
popd
exit /b 0

:failed
echo ERROR: Environment setup failed. Check the output above.
popd
exit /b 1
