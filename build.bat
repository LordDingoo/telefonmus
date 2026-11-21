@echo off
rem Bygger Telefonmus.exe i mappen dist\
cd /d "%~dp0"
python -m pip install --quiet pyinstaller qrcode pillow
python -m PyInstaller --noconfirm --onefile --windowed --name Telefonmus --add-data "pad.html;." telefonmus.py
echo.
echo Faerdig: dist\Telefonmus.exe
