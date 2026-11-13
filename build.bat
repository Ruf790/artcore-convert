@echo off
rem ArtCore Convert: FFmpeg + the .exe (PyInstaller) + the installer (Inno Setup)
rem Needs: Python 3.12/3.13 64-bit (python.org), Inno Setup 6.3+ (jrsoftware.org)
cd /d "%~dp0"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller pillow || goto :err

if not exist ffmpeg\bin\ffmpeg.exe (
  python tools\fetch_ffmpeg.py || goto :err
)
python tools\make_icon.py || goto :err

python -m PyInstaller --noconfirm --clean --windowed ^
  --name ArtCoreConvert ^
  --icon assets\icon.ico ^
  --version-file version_info.txt ^
  --collect-data qtawesome ^
  --exclude-module PySide6.QtQml ^
  --exclude-module PySide6.QtQuick ^
  --exclude-module PySide6.QtQuickWidgets ^
  --exclude-module PySide6.QtQuick3D ^
  --exclude-module PySide6.QtQuickControls2 ^
  --exclude-module PySide6.QtPdf ^
  --exclude-module PySide6.QtPdfWidgets ^
  --exclude-module PySide6.QtWebEngineCore ^
  --exclude-module PySide6.QtWebEngineWidgets ^
  --exclude-module PySide6.QtMultimedia ^
  --exclude-module PySide6.QtMultimediaWidgets ^
  --exclude-module PySide6.QtSql ^
  --exclude-module PySide6.QtTest ^
  --exclude-module PySide6.QtDesigner ^
  --exclude-module PySide6.Qt3DCore ^
  run.py || goto :err

python tools\trim_build.py dist\ArtCoreConvert || goto :err
xcopy /e /i /y ffmpeg dist\ArtCoreConvert\ffmpeg >nul || goto :err
echo.
echo EXE: dist\ArtCoreConvert\ArtCoreConvert.exe

set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo Inno Setup not found - installer skipped.
  goto :done
)
"%ISCC%" setup.iss || goto :err
echo Installer: installer\ArtCoreConvert-Setup-*.exe

:done
pause
exit /b 0

:err
echo BUILD FAILED
pause
exit /b 1
