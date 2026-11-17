@echo off
:: ===========================================================================
::  FalconGuard - build and install
::
::    build.bat              build FalconGuard.exe and install it on this PC
::    build.bat uninstall    open FalconGuard's uninstaller (same as uninstall.bat)
::
::  Right-click this file and choose "Run as administrator".
::  Needs: Windows 10/11, Python 3 from python.org (with tcl/tk), internet for PyInstaller.
::  Put this file next to falcon.py, web.html, logon.mp3, logoff.mp3 and error.mp3.
:: ===========================================================================
setlocal EnableExtensions
title FalconGuard Setup
cd /d "%~dp0"

set "INSTALL=%ProgramFiles%\FalconGuard"
set "EXE=%ProgramFiles%\FalconGuard\FalconGuard.exe"
set "DATA=%ProgramData%\Falcon"
:: build files go to a temporary folder, not next to this file (files made as administrator
:: there would need administrator permission to replace when you download a new version)
set "WORK=%TEMP%\FalconGuard-build"

if /i "%~1"=="uninstall" goto :uninstall

:: ---- administrator rights ---------------------------------------------------
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo  FalconGuard Setup needs administrator rights.
    echo  Close this window, right-click build.bat and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)

echo.
echo  ============================================
echo   FalconGuard Setup
echo  ============================================
echo.
echo   Don't click inside this window: that pauses setup. If the title starts
echo   with "Select", press Esc to carry on.
echo.

:: ---- 1. check the files and Python -----------------------------------------
for %%F in (falcon.py web.html logon.mp3 logoff.mp3 error.mp3) do (
    if not exist "%%F" (
        echo  [X] %%F is missing. Put build.bat in the same folder as falcon.py and the sounds.
        goto :failed
    )
)
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo  [X] Python 3 isn't installed. Install it from https://www.python.org/downloads/
    echo      and tick "Add python.exe to PATH", then run build.bat again.
    goto :failed
)
%PY% -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo  [X] Python is installed without tkinter. Reinstall Python from python.org and keep
    echo      "tcl/tk and IDLE" ticked.
    goto :failed
)
echo  [1/5] Python found.

:: ---- 2. build FalconGuard.exe -----------------------------------------------
echo  [2/5] Checking for PyInstaller...
%PY% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo        Downloading PyInstaller - this can take a few minutes...
    %PY% -m pip install --disable-pip-version-check --timeout 30 --retries 2 pyinstaller
    if errorlevel 1 (
        echo  [X] Couldn't install PyInstaller. Check the internet connection, then run
        echo      build.bat again.
        goto :failed
    )
) else (
    echo        PyInstaller is already installed.
)
if exist "%WORK%" rmdir /s /q "%WORK%"
mkdir "%WORK%"
echo        Drawing the falcon icon...
%PY% falcon.py --make-icon "%WORK%\falcon.ico"
if not exist "%WORK%\falcon.ico" (
    echo  [X] Couldn't make falcon.ico.
    goto :failed
)
echo        Building FalconGuard.exe (this takes a minute)...
%PY% -m PyInstaller --noconfirm --clean --log-level WARN --onedir --windowed --name FalconGuard ^
    --distpath "%WORK%\dist" --workpath "%WORK%\build" --specpath "%WORK%" ^
    --icon "%WORK%\falcon.ico" --add-data "%WORK%\falcon.ico;." ^
    --add-data "%CD%\logon.mp3;." --add-data "%CD%\logoff.mp3;." --add-data "%CD%\error.mp3;." ^
    --add-data "%CD%\web.html;." falcon.py
if errorlevel 1 (
    echo  [X] The build failed. The messages above say why.
    goto :failed
)
if not exist "%WORK%\dist\FalconGuard\FalconGuard.exe" (
    echo  [X] The build didn't produce FalconGuard.exe.
    goto :failed
)
echo        Built FalconGuard.exe

:: ---- 3. the child's account ---------------------------------------------------
echo.
echo  [3/5] Which Windows account does the child use? Accounts on this PC:
net user | findstr /v /c:"command completed" /c:"User accounts for" /c:"-----"
:ask_child
set "CHILD="
set /p "CHILD=        Child's user name: "
if not defined CHILD goto :ask_child
net user "%CHILD%" >nul 2>&1
if errorlevel 1 (
    echo        There's no account called "%CHILD%". Type it exactly as listed above.
    goto :ask_child
)
net localgroup Administrators | findstr /i /x /c:"%CHILD%" >nul
if not errorlevel 1 (
    echo.
    echo  [!] "%CHILD%" is an administrator. An administrator can remove FalconGuard.
    echo      Make it a standard account: Settings ^> Accounts ^> Family ^& other users.
    choice /c YN /m "      Continue anyway"
    if errorlevel 2 goto :failed
)

:: ---- 4. copy the program to Program Files (read-only for standard accounts) ----
echo  [4/5] Installing to %INSTALL%...
taskkill /f /im FalconGuard.exe >nul 2>&1
robocopy "%WORK%\dist\FalconGuard" "%INSTALL%" /MIR /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo  [X] Couldn't copy FalconGuard to %INSTALL%.
    goto :failed
)
icacls "%INSTALL%" /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-545:(OI)(CI)RX" >nul
rmdir /s /q "%WORK%" >nul 2>&1

:: ---- 5. FalconGuard sets itself up -------------------------------------------
:: protection, scheduled tasks, firewall, start-up entry, Start menu, Settings ^> Apps
echo  [5/5] Setting up FalconGuard...
start "" /wait "%EXE%" --setup "%CHILD%"
set "RESULT=%errorlevel%"
echo.
if exist "%DATA%\setup.txt" type "%DATA%\setup.txt"
if not "%RESULT%"=="0" (
    echo.
    echo  [X] Some steps failed - see the list above.
    goto :failed
)

echo.
echo  ============================================
echo   FalconGuard is installed.
echo  ============================================
echo.
echo   Child's account : %CHILD%
echo   Program         : %INSTALL%
echo   Settings        : %DATA%   (read-only except for administrators)
echo.
echo   Next: FalconGuard Settings opens now. Create the supervisor password,
echo   click Turn On on the General tab, and choose your rules.
echo.
echo   Protection runs on %CHILD%'s account: sign in to it (or sign out and back in)
echo   to start it. The General tab shows whether it's running there.
echo   To remove FalconGuard later: Settings ^> Apps, or uninstall.bat.
echo.
start "" "%EXE%"
pause
exit /b 0

:: ---- uninstall ----------------------------------------------------------------
:uninstall
if exist "%EXE%" (
    start "" "%EXE%" --uninstall
) else (
    echo FalconGuard isn't installed on this PC.
    pause
)
exit /b

:failed
echo.
echo  Setup stopped.
pause
exit /b 1
