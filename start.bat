@echo off
rem ===== Starts the booking site on this computer =====
rem Double-click this file: the server starts and the browser opens.
rem To stop the server: close this window.

rem Go to the folder this file is in, wherever it was started from
cd /d "%~dp0"

echo.
echo   Starting the booking site...
echo   Site:   http://localhost:5000
echo   Admin:  http://localhost:5000/admin
echo   To stop: close this window.
echo.

rem Open the browser after 2 seconds, once the server is ready
start "" cmd /c "timeout /t 2 >nul & start http://localhost:5000"

python web.py

rem If the server crashed, keep the window open so the error can be read
echo.
echo   The server stopped. If you see an error above, send a screenshot.
pause
