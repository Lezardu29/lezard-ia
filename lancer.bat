@echo off
rem Double-cliquez pour lancer la Smart AI Gateway (voir lancer.ps1 pour les options).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0lancer.ps1" %*
pause
