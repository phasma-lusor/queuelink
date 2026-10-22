@echo off
REM Builds dist\QueueLink.exe. Needs Python 3.9+ installed (python.org, tick "Add to PATH").
cd /d "%~dp0"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --name QueueLink --icon icon.ico ^
  --add-data "index.html;." ^
  --hidden-import engineio.async_drivers.aiohttp ^
  --collect-submodules qrcode ^
  server.py
echo.
if exist dist\QueueLink.exe (echo Done: dist\QueueLink.exe) else (echo Build failed - see the messages above.)
pause
