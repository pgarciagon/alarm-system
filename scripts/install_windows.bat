@echo off
:: =============================================================================
:: install_windows.bat — Doppelklick-Installer fuer das Alarmsystem
::
:: Rechtsklick -> "Als Administrator ausfuehren"
:: =============================================================================

:: Check for admin rights
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo.
    echo  [!] Bitte als Administrator ausfuehren!
    echo      Rechtsklick auf diese Datei -> "Als Administrator ausfuehren"
    echo.
    pause
    exit /b 1
)

echo.
echo  Alarmsystem wird installiert...
echo  Bitte warten.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& { Invoke-Expression ((New-Object System.Net.WebClient).DownloadString('https://raw.githubusercontent.com/pgarciagon/alarm-system/main/scripts/install_windows.ps1')) }"

if %errorLevel% neq 0 (
    echo.
    echo  [X] Installation fehlgeschlagen.
    echo      Bitte pruefen Sie die Fehlermeldung oben.
    pause
    exit /b 1
)

pause
