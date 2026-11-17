@echo off
:: FalconGuard - uninstall
:: Opens FalconGuard's own uninstaller. Windows asks for administrator rights;
:: the supervisor password isn't needed. (You can also use Settings > Apps.)
if exist "%ProgramFiles%\FalconGuard\FalconGuard.exe" (
    start "" "%ProgramFiles%\FalconGuard\FalconGuard.exe" --uninstall
) else (
    echo FalconGuard isn't installed on this PC.
    pause
)
