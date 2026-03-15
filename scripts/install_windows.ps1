# =============================================================================
# install_windows.ps1 — One-click Alarmsystem installer for Windows 11
#
# Usage (run as Administrator in PowerShell):
#   Set-ExecutionPolicy Bypass -Scope Process -Force
#   irm https://raw.githubusercontent.com/pgarciagon/alarm-system/main/scripts/install_windows.ps1 | iex
#
# Or download and run locally:
#   powershell -ExecutionPolicy Bypass -File install_windows.ps1
#
# What this script does:
#   1. Checks for Python 3.12+; installs it silently via winget if missing
#   2. Installs required Python packages (websockets, keyboard, pygame, pyinstaller)
#   3. Downloads the alarm-system repository as a ZIP from GitHub
#   4. Builds alarm_installer.exe with PyInstaller
#   5. Launches the GUI installer (asks: Server or Client?)
# =============================================================================

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"   # speeds up Invoke-WebRequest

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
$REPO_URL   = "https://github.com/pgarciagon/alarm-system/archive/refs/heads/main.zip"
$REPO_ZIP   = "$env:TEMP\alarm-system.zip"
$REPO_DIR   = "$env:TEMP\alarm-system-main"
$INSTALL_DIR = "$env:ProgramFiles\AlarmSystem"
$MIN_PYTHON  = [version]"3.12"

# ---------------------------------------------------------------------------
# Helper: coloured output
# ---------------------------------------------------------------------------
function Write-Step  { param($msg) Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-OK    { param($msg) Write-Host "    [OK] $msg" -ForegroundColor Green }
function Write-Warn  { param($msg) Write-Host "    [!]  $msg" -ForegroundColor Yellow }
function Write-Fail  { param($msg) Write-Host "    [X]  $msg" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
Clear-Host
Write-Host ""
Write-Host "  ==============================================" -ForegroundColor Red
Write-Host "        ALARMSYSTEM  —  Windows Installer       " -ForegroundColor White
Write-Host "  ==============================================" -ForegroundColor Red
Write-Host ""
Write-Host "  Dieses Skript installiert das Alarmsystem" -ForegroundColor Gray
Write-Host "  automatisch auf diesem PC." -ForegroundColor Gray
Write-Host ""

# ---------------------------------------------------------------------------
# Step 1: Find or install Python 3.12+
# ---------------------------------------------------------------------------
Write-Step "Python 3.12+ wird gesucht..."

function Find-Python {
    $candidates = @(
        "python",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "C:\Python312\python.exe",
        "C:\Python313\python.exe"
    )
    foreach ($c in $candidates) {
        try {
            $ver = & $c --version 2>&1
            if ($ver -match "Python (\d+\.\d+)") {
                $v = [version]$Matches[1]
                if ($v -ge $MIN_PYTHON) {
                    return $c
                }
            }
        } catch {}
    }
    return $null
}

$python = Find-Python

if ($null -eq $python) {
    Write-Warn "Python 3.12+ nicht gefunden. Wird installiert via winget..."
    try {
        winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
        # Reload PATH
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("PATH", "User")
        $python = Find-Python
        if ($null -eq $python) {
            # winget installs to a versioned path; try common location
            $python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
        }
    } catch {
        Write-Fail "Python konnte nicht installiert werden. Bitte manuell installieren: https://www.python.org/downloads/"
    }
}

try {
    $verOut = & $python --version 2>&1
    Write-OK "Gefunden: $verOut  ($python)"
} catch {
    Write-Fail "Python nicht ausfuehrbar: $python"
}

# ---------------------------------------------------------------------------
# Step 2: Install Python packages
# ---------------------------------------------------------------------------
Write-Step "Python-Pakete werden installiert..."

$packages = @("websockets", "keyboard", "pygame", "tomli", "pyinstaller")
foreach ($pkg in $packages) {
    Write-Host "    pip install $pkg..." -NoNewline
    $result = & $python -m pip install --quiet --upgrade $pkg 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host " OK" -ForegroundColor Green
    } else {
        Write-Host " FEHLER" -ForegroundColor Red
        Write-Host $result
        Write-Fail "Paket '$pkg' konnte nicht installiert werden."
    }
}

# ---------------------------------------------------------------------------
# Step 3: Download repository
# ---------------------------------------------------------------------------
Write-Step "Alarmsystem-Code wird heruntergeladen..."

if (Test-Path $REPO_DIR) {
    Remove-Item $REPO_DIR -Recurse -Force
}

try {
    Invoke-WebRequest -Uri $REPO_URL -OutFile $REPO_ZIP -UseBasicParsing
    Write-OK "Download abgeschlossen."
} catch {
    Write-Fail "Download fehlgeschlagen: $_`nBitte Internetverbindung pruefen."
}

Write-Host "    Entpacke..."
Expand-Archive -Path $REPO_ZIP -DestinationPath $env:TEMP -Force
Remove-Item $REPO_ZIP -Force

if (-not (Test-Path $REPO_DIR)) {
    Write-Fail "Entpacken fehlgeschlagen — Ordner nicht gefunden: $REPO_DIR"
}
Write-OK "Entpackt nach: $REPO_DIR"

# ---------------------------------------------------------------------------
# Step 4: Build alarm_installer.exe with PyInstaller
# ---------------------------------------------------------------------------
Write-Step "alarm_installer.exe wird kompiliert (dauert ca. 1-2 Min.)..."

Push-Location $REPO_DIR

# Fix path separator in spec file for Windows (: -> ;)
$specFile = "scripts\alarm_installer.spec"
(Get-Content $specFile -Raw) | Set-Content $specFile

try {
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --uac-admin `
        $specFile `
        2>&1 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }

    if ($LASTEXITCODE -ne 0) {
        Write-Fail "PyInstaller fehlgeschlagen (Exit-Code: $LASTEXITCODE)"
    }
} catch {
    Write-Fail "PyInstaller-Fehler: $_"
}

$exePath = "$REPO_DIR\dist\alarm_installer.exe"
if (-not (Test-Path $exePath)) {
    Write-Fail "alarm_installer.exe wurde nicht erstellt. Siehe Ausgabe oben."
}

Write-OK "Erstellt: $exePath"

# ---------------------------------------------------------------------------
# Step 5: Copy installer to ProgramFiles and launch it
# ---------------------------------------------------------------------------
Write-Step "Installer wird nach $INSTALL_DIR kopiert..."

if (-not (Test-Path $INSTALL_DIR)) {
    New-Item -ItemType Directory -Path $INSTALL_DIR -Force | Out-Null
}
Copy-Item $exePath "$INSTALL_DIR\alarm_installer.exe" -Force
Write-OK "Kopiert nach: $INSTALL_DIR\alarm_installer.exe"

Pop-Location

# ---------------------------------------------------------------------------
# Step 6: Launch the GUI installer
# ---------------------------------------------------------------------------
Write-Step "GUI-Installer wird gestartet..."
Write-Host ""
Write-Host "  +--------------------------------------------------+" -ForegroundColor Yellow
Write-Host "  |  Im naechsten Fenster waehlen Sie:               |" -ForegroundColor Yellow
Write-Host "  |    SERVER  — fuer den zentralen Praxis-PC        |" -ForegroundColor Yellow
Write-Host "  |    CLIENT  — fuer jeden Patientenzimmer-PC       |" -ForegroundColor Yellow
Write-Host "  +--------------------------------------------------+" -ForegroundColor Yellow
Write-Host ""

Start-Process "$INSTALL_DIR\alarm_installer.exe" -Wait

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "  Installation abgeschlossen." -ForegroundColor Green
Write-Host "  Das Alarmsystem startet automatisch beim naechsten Systemstart." -ForegroundColor Gray
Write-Host ""
