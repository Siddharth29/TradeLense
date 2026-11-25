@echo off
setlocal
cd /d "%~dp0"

set "TRADELENS_OFFLINE=true"
set "TRADELENS_MT5_AUTOCONNECT=false"
set "VITE_API_BASE_URL=http://127.0.0.1:8000"

if not exist "apps\frontend\node_modules\.bin\vite.cmd" (
    call npm --prefix apps\frontend ci
    if errorlevel 1 exit /b 1
)

start "TradeLense backend" cmd /k "python -m uvicorn apps.backend.api:app --host 127.0.0.1 --port 8000"
start "TradeLense frontend" cmd /k "npm --prefix apps\frontend run dev -- --host 127.0.0.1"
endlocal
