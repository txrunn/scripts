@echo off
:: =====================================================================
::  Gaming Services Session Toggle
::  Installs Gaming Services + Xbox Identity Provider before a Forza
::  session, and fully removes them (plus leftover registry entries)
::  once you're done. Keep this file next to GamingServices.ps1.
:: =====================================================================

:: ---- Auto-elevate to Administrator ----
>nul 2>&1 "%SYSTEMROOT%\system32\cacls.exe" "%SYSTEMROOT%\system32\config\system"
if '%errorlevel%' NEQ '0' (
    echo Requesting administrative privileges...
    goto UACPrompt
) else ( goto gotAdmin )

:UACPrompt
    echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\getadmin.vbs"
    echo UAC.ShellExecute "%~s0", "", "", "runas", 1 >> "%temp%\getadmin.vbs"
    "%temp%\getadmin.vbs"
    exit /B

:gotAdmin
    if exist "%temp%\getadmin.vbs" del "%temp%\getadmin.vbs"
    pushd "%~dp0"

:: ---- Make sure the companion script is actually here ----
if not exist "%~dp0GamingServices.ps1" (
    echo.
    echo [!] GamingServices.ps1 was not found next to this .bat file.
    echo     Keep both files together in the same folder.
    echo.
    pause
    exit /B
)

:MENU
cls
echo =======================================================
echo   GAMING SERVICES SESSION TOGGLE  ^(Forza Horizon^)
echo =======================================================
echo.
echo   [1] INSTALL  - run before you play
echo   [2] REMOVE   - run after your session
echo   [3] STATUS   - see what's currently installed
echo   [4] Exit
echo.
echo =======================================================
set "choice="
set /p "choice=Enter your choice (1-4): "

if "%choice%"=="1" goto INSTALL
if "%choice%"=="2" goto REMOVE
if "%choice%"=="3" goto STATUS
if "%choice%"=="4" exit /B
goto MENU

:INSTALL
cls
echo [+] Installing Gaming Services + Xbox Identity Provider...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0GamingServices.ps1" -Action Install
echo.
pause
goto MENU

:REMOVE
cls
echo This removes Gaming Services, Xbox Identity Provider, and their
echo leftover registry entries. Close Forza first if it's still running.
echo.
set /p "confirm=Continue? (Y/N): "
if /i not "%confirm%"=="Y" goto MENU
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0GamingServices.ps1" -Action Remove
echo.
pause
goto MENU

:STATUS
cls
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0GamingServices.ps1" -Action Status
echo.
pause
goto MENU
