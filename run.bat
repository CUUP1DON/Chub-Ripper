@echo off
cd /d "%~dp0"

REM Try python from PATH first (works if Python is properly installed)
where python >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON=python
    goto :install
)

REM Fallback: common install locations
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "C:\Program Files\Python313\python.exe"
    "C:\Program Files\Python312\python.exe"
    "C:\Program Files\Python311\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
) do (
    if exist %%P ( set PYTHON=%%P & goto :install )
)

echo Could not find Python. Please install Python 3.11+ and add it to your PATH.
pause
exit /b 1

:install
echo Installing / updating requirements...
"%PYTHON%" -m pip install -q -r requirements.txt
if %errorlevel% neq 0 (
    echo pip install failed. See above for details.
    pause
    exit /b 1
)

REM Install Playwright browsers on first run (skips if already installed)
"%PYTHON%" -m playwright install chromium --with-deps >nul 2>&1

:run
"%PYTHON%" chub_ripper.py
if %errorlevel% neq 0 (
    echo.
    echo Script exited with an error. See above for details.
    pause
)
