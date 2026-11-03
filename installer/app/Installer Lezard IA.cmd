@echo off
rem Installation de Lezard IA depuis le zip (sans installateur) : lancée par un double-clic dans le dossier extrait.
chcp 65001 >nul
cd /d "%~dp0"
if not exist "python\python.exe" (
  echo Le zip n'est pas extrait : clic droit sur le zip, « Extraire tout... », puis lancez ce fichier depuis le
  echo dossier extrait.
  pause
  exit /b 1
)
"python\python.exe" lezard.py install
if errorlevel 1 (
  echo.
  echo L'installation n'a pas abouti : voir le message ci-dessus.
  pause
)
