<#
  Lance la Smart AI Gateway et tout ce qui l'accompagne, à la chaîne.

    .\lancer.ps1              démarrage normal
    .\lancer.ps1 -Tests       lance d'abord les tests automatiques
    .\lancer.ps1 -Port 8080   autre port
    .\lancer.ps1 -NoBrowser   sans ouvrir le navigateur
    .\lancer.ps1 -Docker      via Docker Compose (docker-compose.yml)
    .\lancer.ps1 -Lan         joignable par les autres appareils du réseau local (téléphone, autre PC) ;
                              clé requise pour eux. Équivaut à GATEWAY_BIND=0.0.0.0 dans .env.
    .\lancer.ps1 -Https       comme -Lan, mais CHIFFRÉ (recommandé) : HTTPS sur le port 8443 (-TlsPort) avec un
                              certificat local ; installer une fois <adresse>/ca.crt sur chaque appareil.

  Démarrage automatique avec Windows (18.1), à l'ouverture de votre session, dans UNE fenêtre réduite
  « Lezard IA » (passerelle, tunnel, Ollama, SearXNG, ComfyUI) ; la fermer arrête tout :
    .\lancer.ps1 -Install     installe et démarre le service (mêmes options : -Port, -Lan, -Https, -TlsPort)
    .\lancer.ps1 -Status      état du service        .\lancer.ps1 -Stop        arrête le service
    .\lancer.ps1 -Uninstall   arrête et retire le démarrage automatique
  DAILY_SCRIPT dans .env : script PowerShell lancé une fois par jour par le service (ex. sauvegarde des documents).
  Le service (-Service, lancé par la tâche planifiée) relance la gateway si elle s'arrête, et attend qu'une gateway
  lancée à la main libère le port. Journaux : data\logs\service.log et data\logs\gateway.log.

  Mise à jour (18.3) : .\lancer.ps1 -Update   sauvegarde la base, récupère la nouvelle version (git), installe les
                              dépendances, lance les tests, redémarre le service ; si un test échoue, retour à la
                              version précédente, sans rien avoir modifié dans la base.

  Double authentification de l'administration (20.1) perdue (téléphone perdu, plus de code de secours) :
    .\lancer.ps1 -ResetTotp  la désactive et ferme les sessions ; à exécuter SUR ce PC. Réactivez-la ensuite dans
                              l'administration (section Double authentification).

  Étapes : Python + dépendances -> fichier .env (créé au 1er lancement) -> Ollama (si installé)
  -> gateway -> agent de pont (si jeton) -> navigateur.
  Ctrl+C arrête tout ce que le script a démarré.
#>
param([int]$Port = 8000, [switch]$Tests, [switch]$NoBrowser, [switch]$Docker, [switch]$Lan, [switch]$Https,
      [int]$TlsPort = 8443, [switch]$Service, [switch]$Install, [switch]$Uninstall, [switch]$Stop, [switch]$Status,
      [switch]$ResetTotp, [switch]$Update)

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$started = New-Object System.Collections.ArrayList     # processus lancés par ce script (arrêtés à la fin)
$logs = Join-Path $PSScriptRoot "data\logs"
New-Item -ItemType Directory -Force $logs | Out-Null

function Step($m) { Write-Host "`n> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  [x]  $m" -ForegroundColor Red; exit 1 }

function Test-Url($url, $timeout = 2) {
  try { $null = Invoke-WebRequest -UseBasicParsing $url -TimeoutSec $timeout; return $true } catch { return $false }
}

function Get-LanAddresses {
  # adresses IPv4 de la machine sur le réseau (hors boucle locale, APIPA et cartes virtuelles WSL/Hyper-V)
  try {
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
      Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and
                     $_.InterfaceAlias -notlike "vEthernet*" -and $_.InterfaceAlias -notlike "*WSL*" -and
                     $_.PrefixOrigin -in @("Dhcp", "Manual") } |
      Select-Object -ExpandProperty IPAddress
  } catch { @() }
}

function Open-FirewallPort($port) {
  # réseaux PRIVÉS seulement (maison, bureau) : jamais sur un Wi-Fi public
  $name = "Smart AI Gateway (TCP $port)"
  if (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue) { Ok "pare-feu : règle « $name » présente"; return }
  $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
  if ($admin) {
    New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow -Profile Private | Out-Null
    Ok "pare-feu : port $port ouvert sur les réseaux privés"
  } else {
    Warn "pare-feu : les autres appareils seront bloqués tant que le port $port n'est pas ouvert. Une fois, dans PowerShell administrateur :"
    Write-Host "       New-NetFirewallRule -DisplayName '$name' -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow -Profile Private"
  }
  $public = Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object { $_.NetworkCategory -eq "Public" }
  if ($public) {
    Warn "le réseau « $(($public | Select-Object -First 1).Name) » est classé Public : Windows y bloque la gateway. Passez-le en Privé (Paramètres > Réseau) si c'est votre réseau."
  }
}

function New-Secret($prefix) {
  $bytes = New-Object byte[] 32                        # ~43 caractères : au-delà des minimums (9.2)
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  return $prefix + ([Convert]::ToBase64String($bytes) -replace '[+/=]', '')
}

# ---------- démarrage automatique (18.1) ----------
$TaskName = if ($Port -eq 8000) { "Smart AI Gateway" } else { "Smart AI Gateway ($Port)" }
$stateFile = Join-Path $PSScriptRoot "data\service-$Port.json"
$startupLink = Join-Path ([Environment]::GetFolderPath("Startup")) "$TaskName.lnk"

function Save-ServiceState {
  # processus du service : arrêter la tâche planifiée ne tue que ce script, pas la gateway qu'il a lancée.
  # L'heure de démarrage de chaque processus évite de tuer un autre programme qui aurait repris le même PID.
  if (-not $Service) { return }
  $procs = @(Get-Process -Id $PID) + @($started | Where-Object { $_ -and -not $_.HasExited })
  @{ port = $Port; processes = @($procs | ForEach-Object { @{ id = $_.Id; start = $_.StartTime.ToString("yyyyMMddHHmmssfffffff") } }) } |
    ConvertTo-Json -Depth 3 | Out-File -FilePath $stateFile -Encoding utf8
}

function Stop-GatewayService {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  }
  return Stop-RecordedProcesses
}

function Stop-RecordedProcesses {
  # processus d'une exécution précédente du service (fenêtre fermée, arrêt brutal) : sans ce nettoyage, une gateway
  # restée seule garderait le port et le nouveau service l'attendrait sans fin
  $n = 0
  if (Test-Path $stateFile) {
    $state = Get-Content $stateFile -Raw -Encoding utf8 | ConvertFrom-Json
    # enfants (gateway, Ollama, agent de pont) d'abord, le script en dernier
    foreach ($p in @($state.processes | Select-Object -Skip 1) + @($state.processes | Select-Object -First 1)) {
      $proc = Get-Process -Id $p.id -ErrorAction SilentlyContinue
      if ($proc -and $proc.Id -ne $PID -and $proc.StartTime.ToString("yyyyMMddHHmmssfffffff") -eq "$($p.start)") {
        # ses propres enfants d'abord (tunnel ngrok / cloudflared lancé par la gateway) : un arrêt forcé ne les
        # emporte pas sous Windows, ils resteraient seuls en ligne. Seulement ceux nés après lui (PID réutilisé).
        Get-CimInstance Win32_Process -Filter "ParentProcessId = $($proc.Id)" -ErrorAction SilentlyContinue |
          Where-Object { $_.CreationDate -ge $proc.StartTime } |
          ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        $n++
      }
    }
    Remove-Item $stateFile -Force -ErrorAction SilentlyContinue
  }
  return $n
}

if ($Status) {
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($task) { Write-Host "  Démarrage automatique : tâche planifiée « $TaskName » ($($task.State))" }
  elseif (Test-Path $startupLink) { Write-Host "  Démarrage automatique : raccourci du dossier Démarrage ($startupLink)" }
  else { Write-Host "  Démarrage automatique : non installé (.\lancer.ps1 -Install)" }
  $health = if (Test-Url "http://127.0.0.1:$Port/health") { "répond sur http://localhost:$Port" } else { "ne répond pas" }
  Write-Host "  Gateway : $health"
  Write-Host "  Journaux : $logs\service.log, $logs\gateway.log"
  exit 0
}
if ($Stop -or $Uninstall) {
  $n = Stop-GatewayService
  Write-Host "  [ok] service arrêté ($n processus)" -ForegroundColor Green
  if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item $startupLink -Force -ErrorAction SilentlyContinue
    Write-Host "  [ok] démarrage automatique retiré" -ForegroundColor Green
  }
  exit 0
}
if (($Service -or $Install) -and $Docker) {
  Fail "-Install et -Service sont inutiles avec -Docker : Docker relance déjà le conteneur (restart: unless-stopped)."
}
if ($Service) {
  # sans surveillance : pas de navigateur, pas de tests, tout dans un journal (gardé sous 5 Mo)
  $NoBrowser = $true
  $Tests = $false
  $serviceLog = Join-Path $logs "service.log"
  if ((Test-Path $serviceLog) -and (Get-Item $serviceLog).Length -gt 5MB) { Move-Item $serviceLog "$serviceLog.1" -Force }
  Start-Transcript -Path $serviceLog -Append | Out-Null
  # un seul terminal pour tout Lezard IA : passerelle, tunnel, Ollama, SearXNG, ComfyUI y sont attachés
  $Host.UI.RawUI.WindowTitle = "Lezard IA - fermer cette fenetre arrete tout"
  $old = Stop-RecordedProcesses
  if ($old) { Warn "$old processus d'une exécution précédente arrêtés" }
}

Write-Host "`n  Smart AI Gateway — lancement" -ForegroundColor White

# ---------- 1. configuration (.env) ----------
Step "Configuration"
$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile)) {
  $master = New-Secret "gw-"
  $testAccount = New-Secret "ct-"
  @"
# Configuration locale de la Smart AI Gateway (créée par lancer.ps1). Ne pas partager ce fichier.

# --- accès ---
GATEWAY_MASTER_KEY=$master
# comptes clients : {"<jeton>": "free"}
GATEWAY_KEYS={"$testAccount": "free"}

# --- fournisseurs cloud (optionnels ; Groq propose une clé gratuite) ---
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
MISTRAL_API_KEY=
GROQ_API_KEY=
OPENROUTER_API_KEY=

# --- fenêtre de contexte d'Ollama (défaut d'Ollama : 4096 tokens) ---
# plus grande = messages plus longs, mais plus de mémoire. Ex. : OLLAMA_CONTEXT_LENGTH=32768
OLLAMA_CONTEXT_LENGTH=

# --- simuler l'instance hébergée (7.4) ---
GATEWAY_HOSTED=false

# --- agent de pont : jeton gwb_… créé dans le tableau de bord (section Ponts) ---
GATEWAY_BRIDGE_TOKEN=
BRIDGE_TARGET=http://localhost:11434/v1
"@ | Out-File -FilePath $envFile -Encoding utf8
  Ok "fichier .env créé (clé maître générée) — complétez-le pour ajouter des clés de fournisseurs"
}
# ancien défaut devinable « compte-test » (exposé dès qu'un tunnel est ouvert) : remplacé par un jeton aléatoire
$envText = Get-Content $envFile -Raw -Encoding utf8
if ($envText -match '"compte-test"') {
  $envText.Replace('"compte-test"', '"' + (New-Secret "ct-") + '"') | Out-File -FilePath $envFile -Encoding utf8 -NoNewline
  Ok "jeton du compte de test « compte-test » remplacé par un jeton aléatoire dans .env"
}
# charge le .env dans ce processus (hérité par la gateway et l'agent) ; valeurs vides ignorées
$loadedEnv = New-Object System.Collections.ArrayList     # noms chargés (retirés le temps des tests de -Update)
foreach ($line in Get-Content $envFile -Encoding utf8) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$' -and $Matches[2] -ne "") {
    $value = $Matches[2]
    if ($value.Length -ge 2 -and (($value[0] -eq '"' -and $value[-1] -eq '"') -or ($value[0] -eq "'" -and $value[-1] -eq "'"))) {
      $value = $value.Substring(1, $value.Length - 2)
    }
    [Environment]::SetEnvironmentVariable($Matches[1], $value, "Process")
    $null = $loadedEnv.Add($Matches[1])
  }
}
Ok ".env chargé"

if ($Update) {
  # mise à jour assistée (18.3) : sauvegarde -> git -> dépendances -> tests -> redémarrage ; retour arrière si échec
  Step "Mise à jour"
  if (-not (Get-Command git -ErrorAction SilentlyContinue) -or -not (Test-Path (Join-Path $PSScriptRoot ".git"))) {
    Fail "-Update demande une copie git du projet (git clone). Image Docker : docker compose pull, puis up -d."
  }
  $env:GIT_TERMINAL_PROMPT = "0"
  if (git status --porcelain --untracked-files=no) {
    Fail "fichiers du projet modifiés localement (git status) : enregistrez-les à part (git stash) puis relancez."
  }
  $before = (git rev-parse HEAD).Trim()
  $beforeShort = (git rev-parse --short HEAD).Trim()
  git fetch --quiet
  if ($LASTEXITCODE -ne 0) { Fail "dépôt d'origine injoignable (réseau, identifiants git) : rien n'a été modifié." }
  $behind = [int](git rev-list --count "HEAD..@{u}")
  if ($behind -eq 0) { Ok "déjà à jour (version $beforeShort)"; exit 0 }
  Write-Host "  $behind nouveauté(s) :"
  git log --format="    %h  %s" -n 15 "HEAD..@{u}"

  Write-Host "  1/4 sauvegarde de la base..."
  & python -m gateway.backup create
  if ($LASTEXITCODE -ne 0) { Fail "sauvegarde impossible : mise à jour annulée, rien n'a été modifié." }

  Write-Host "  2/4 récupération de la nouvelle version..."
  git merge --ff-only --quiet "@{u}"
  if ($LASTEXITCODE -ne 0) { Fail "fusion impossible (branche locale divergente) : rien n'a été modifié." }
  $after = (git rev-parse --short HEAD).Trim()

  Write-Host "  3/4 dépendances..."
  & python -m pip install --disable-pip-version-check -q -r requirements-dev.txt
  $ok = $LASTEXITCODE -eq 0
  if ($ok) {
    Write-Host "  4/4 tests automatiques (quelques minutes)..."
    # jamais avec la configuration réelle : une variable du .env (GATEWAY_DB…) ferait travailler les tests sur
    # la vraie base. Retirées le temps des tests, remises ensuite.
    $saved = @{}
    foreach ($name in $loadedEnv) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
                                    [Environment]::SetEnvironmentVariable($name, $null, "Process") }
    & python -m pytest -q -x -p no:cacheprovider
    $ok = $LASTEXITCODE -eq 0
    foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process") }
  }
  if (-not $ok) {
    Warn "échec : retour à la version $beforeShort"
    git reset --hard --quiet $before
    & python -m pip install --disable-pip-version-check -q -r requirements-dev.txt
    Fail "mise à jour annulée : la version $beforeShort est conservée et la base n'a pas été modifiée (la gateway en cours continue de tourner)."
  }
  Ok "version $after installée et testée (avant : $beforeShort)"

  # redémarrage : le service s'il est installé ; sinon, à faire à la main
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($task -or (Test-Path $startupLink)) {
    $null = Stop-GatewayService
    if ($task) {
      Start-ScheduledTask -TaskName $TaskName
    } else {
      $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($startupLink)
      Start-Process powershell.exe -ArgumentList $lnk.Arguments -WorkingDirectory $PSScriptRoot -WindowStyle Minimized
    }
    Write-Host "  Redémarrage du service..." -NoNewline
    for ($i = 0; $i -lt 90 -and -not (Test-Url "http://127.0.0.1:$Port/health"); $i++) { Start-Sleep 1; Write-Host "." -NoNewline }
    Write-Host ""
    if (Test-Url "http://127.0.0.1:$Port/health") { Ok "gateway redémarrée sur http://localhost:$Port" }
    else { Warn "la gateway ne répond pas encore : voir $logs\service.log et $logs\gateway.log" }
  } elseif (Test-Url "http://127.0.0.1:$Port/health") {
    Warn "une gateway lancée à la main tourne encore avec l'ancienne version : arrêtez-la (Ctrl+C dans sa fenêtre) puis relancez .\lancer.ps1"
  } else {
    Ok "lancez la gateway : .\lancer.ps1"
  }
  exit 0
}

if ($ResetTotp) {
  # sur le PC seulement (accès au fichier de la base) : jamais possible à distance
  Step "Double authentification"
  & python -m gateway.totp reset
  if ($LASTEXITCODE -ne 0) { Fail "réinitialisation impossible (voir le message ci-dessus)" }
  exit 0
}

# ---------- réseau local (12.1) ----------
if ($Https -and $Docker) { Warn "-Https n'est pas disponible avec -Docker : utilisez Tailscale ou deploy/ (Caddy)."; $Https = $false }
if ($Https) { $Lan = $true; $env:GATEWAY_TLS_PORT = "$TlsPort" }    # chiffrement sur le réseau local (12.7)
if ($Lan) { $env:GATEWAY_BIND = "0.0.0.0" }
$bind = if ($env:GATEWAY_BIND) { $env:GATEWAY_BIND } else { "127.0.0.1" }
$env:GATEWAY_BIND = $bind
$lanMode = $bind -notin @("127.0.0.1", "localhost", "::1")
$env:GATEWAY_PORT = "$Port"                            # le tunnel (accès distant) vise le bon port
$lanUrls = @()
if ($lanMode) {
  Step "Réseau local"
  $scheme, $lanPort = if ($Https) { "https", $TlsPort } else { "http", $Port }
  $lanUrls = @(if ($bind -eq "0.0.0.0") { Get-LanAddresses } else { $bind }) | ForEach-Object { "${scheme}://${_}:$lanPort" }
  if ($lanUrls) { Ok "joignable sur $($lanUrls -join ', ') (clé requise depuis les autres appareils)" }
  else { Warn "aucune adresse réseau local trouvée (machine hors réseau ?)" }
  if (-not $Https) {
    Warn "connexion NON chiffrée : sur le Wi-Fi, clés et conversations sont lisibles. Préférez .\lancer.ps1 -Https"
  }
  if (-not $Docker) { Open-FirewallPort $lanPort }       # -Https : seul le port HTTPS est ouvert au réseau
}

# ---------- variante Docker ----------
if ($Docker) {
  Step "Docker Compose"
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail "Docker introuvable : installez Docker Desktop (https://www.docker.com/products/docker-desktop/)." }
  & docker compose up -d --build
  if ($LASTEXITCODE -ne 0) { Fail "docker compose a échoué (voir ci-dessus)." }
  Write-Host "  Attente de la gateway..." -NoNewline
  for ($i = 0; $i -lt 60 -and -not (Test-Url "http://localhost:$Port/health"); $i++) { Start-Sleep 1; Write-Host "." -NoNewline }
  Write-Host ""
  if (-not $NoBrowser) { Start-Process "http://localhost:$Port" }
  Ok "gateway en conteneur sur http://localhost:$Port — clé maître : GATEWAY_MASTER_KEY dans $envFile"
  if ($lanUrls) { Ok "réseau local : $($lanUrls -join ', ')" }
  Write-Host "  Journaux : docker compose logs -f gateway    Arrêt : docker compose down"
  exit 0
}

# ---------- 2. Python et dépendances ----------
Step "Python"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Fail "Python introuvable : installez Python 3.10+ (https://www.python.org/downloads/) en cochant « Add to PATH »." }
& python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { Fail "Python 3.10 ou plus récent requis (version actuelle : $(& python --version))." }
Ok (& python --version)
& python -c "import fastapi, uvicorn, httpx, websockets, cryptography, fpdf, pypdf, multipart" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "  Installation des dépendances (requirements.txt)..."
  & python -m pip install -r requirements.txt
  if ($LASTEXITCODE -ne 0) { Fail "Installation des dépendances impossible." }
}
Ok "dépendances présentes"
& python -c "import faster_whisper" 2>$null
if ($LASTEXITCODE -ne 0 -and -not $Service) {
  Write-Host "  Installation de la dictée vocale locale (requirements-local.txt, facultatif)..."
  & python -m pip install -r requirements-local.txt
  if ($LASTEXITCODE -ne 0) { Warn "dictée locale non installée : la dictée utilisera un fournisseur cloud s'il y en a un." }
}
& python -c "import kokoro_onnx" 2>$null
if ($LASTEXITCODE -ne 0 -and -not $Service) {
  Write-Host "  Installation de la lecture vocale locale (requirements-local.txt, facultatif)..."
  & python -m pip install -r requirements-local.txt
  if ($LASTEXITCODE -ne 0) { Warn "lecture vocale locale non installée : le bouton « Écouter » utilisera un fournisseur cloud s'il y en a un." }
}
& python -c "from gateway.providers import local_tts; import sys; sys.exit(0 if local_tts.installed() else 1)" 2>$null
if ($LASTEXITCODE -ne 0 -and -not $Service) {   # jamais téléchargé d'office (14.4)
  Warn "lecture vocale locale : modèle Kokoro absent (~350 Mo). Pour l'installer : python -m gateway.providers.local_tts download"
}

if ($Tests) {
  Step "Tests automatiques"
  & python -c "import pytest, httpx2, ruff" 2>$null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "  Installation des outils de test (requirements-dev.txt)..."
    & python -m pip install -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { Fail "Installation des outils de test impossible." }
  }
  & python -m pytest -q -p no:warnings
  if ($LASTEXITCODE -ne 0) { Fail "Des tests échouent : lancement annulé." }
  Ok "tous les tests passent"
  & python -m ruff check --quiet .              # qualité de code (15.3) : signalée, ne bloque pas le lancement
  if ($LASTEXITCODE -ne 0) { Warn "ruff signale des problèmes de code (python -m ruff check .)" } else { Ok "lint : aucun problème" }
}

# ---------- installation du démarrage automatique (18.1) ----------
if ($Install) {
  Step "Démarrage automatique"
  # mêmes options que cette commande ; -WindowStyle Minimized avant -File : une seule fenêtre, réduite
  $serviceArgs = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$PSCommandPath`" -Service -Port $Port"
  if ($Https) { $serviceArgs += " -Https -TlsPort $TlsPort" } elseif ($Lan) { $serviceArgs += " -Lan" }
  $user = "$env:USERDOMAIN\$env:USERNAME"
  $null = Stop-GatewayService                          # réinstallation : l'ancien service s'arrête d'abord
  try {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $serviceArgs -WorkingDirectory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $trigger.Delay = "PT30S"                            # laisse le réseau et Ollama démarrer
    # aucune limite de durée ; relancée si elle échoue ; une seule instance
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
      -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    # votre compte, sans droits administrateur : Ollama et les fichiers du projet sont les vôtres
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
      -Description "Smart AI Gateway, démarrée à l'ouverture de session (lancer.ps1 -Install)" -Force -ErrorAction Stop | Out-Null
    Remove-Item $startupLink -Force -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $TaskName
    Ok "tâche planifiée « $TaskName » : démarrage à chaque ouverture de session"
  } catch {
    Warn "tâche planifiée refusée ($($_.Exception.Message.Trim())) : raccourci du dossier Démarrage à la place"
    $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($startupLink)
    $lnk.TargetPath = "powershell.exe"
    $lnk.Arguments = $serviceArgs
    $lnk.WorkingDirectory = $PSScriptRoot
    $lnk.WindowStyle = 7                                # réduite
    $lnk.Save()
    Start-Process powershell.exe -ArgumentList $serviceArgs -WorkingDirectory $PSScriptRoot -WindowStyle Minimized
    Ok "raccourci $startupLink : démarrage à chaque ouverture de session"
  }
  if (Test-Url "http://127.0.0.1:$Port/health") {
    Ok "une gateway tourne déjà sur le port $Port : le service prendra le relais dès qu'elle s'arrêtera"
  } else {
    Write-Host "  Attente de la gateway..." -NoNewline
    for ($i = 0; $i -lt 90 -and -not (Test-Url "http://127.0.0.1:$Port/health"); $i++) { Start-Sleep 1; Write-Host "." -NoNewline }
    Write-Host ""
    if (Test-Url "http://127.0.0.1:$Port/health") { Ok "gateway prête sur http://localhost:$Port" }
    else { Warn "la gateway ne répond pas encore : voir $logs\service.log et $logs\gateway.log" }
  }
  Write-Host "  État : .\lancer.ps1 -Status    Arrêt : .\lancer.ps1 -Stop    Retrait : .\lancer.ps1 -Uninstall"
  Write-Host "  La gateway démarre à l'OUVERTURE de votre session Windows (pas avant)."
  if (-not $NoBrowser) { Start-Process "http://localhost:$Port" }
  exit 0
}

# ---------- 3. port libre ? ----------
if ($Service) {
  # gateway lancée à la main, ou autre programme : le service attend que le port se libère, puis prend le relais
  $announced = $false
  while ((Test-Url "http://127.0.0.1:$Port/health") -or
         (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
    if (-not $announced) { Warn "port $Port déjà utilisé : le service attend qu'il se libère"; $announced = $true }
    Start-Sleep 10
  }
}
if (Test-Url "http://127.0.0.1:$Port/health") {
  Warn "une gateway répond déjà sur le port $Port : ouverture du navigateur seulement."
  if (-not $NoBrowser) { Start-Process "http://localhost:$Port" }
  exit 0
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
  Fail "le port $Port est occupé par un autre programme : relancez avec -Port 8080 par exemple."
}

try {
  # ---------- 4. modèles locaux (Ollama) ----------
  Step "Modèles locaux"
  # 5 s : à l'ouverture de session, Ollama répond lentement ; le croire absent lancerait un 2e « ollama serve »
  $ollamaUp = Test-Url "http://localhost:11434/api/tags" 5
  if (-not $ollamaUp -and (Get-Command ollama -ErrorAction SilentlyContinue)) {
    $p = Start-Process ollama -ArgumentList "serve" -NoNewWindow -PassThru `
      -RedirectStandardOutput "$logs\ollama.out.log" -RedirectStandardError "$logs\ollama.log"
    [void]$started.Add($p)
    Save-ServiceState
    for ($i = 0; $i -lt 15 -and -not $ollamaUp; $i++) { Start-Sleep 1; $ollamaUp = Test-Url "http://localhost:11434/api/tags" }
  }
  if ($ollamaUp) {
    $models = (Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 3).models
    if ($models) { Ok "Ollama : $(($models | ForEach-Object { $_.name }) -join ', ')" }
    else { Warn "Ollama tourne mais n'a aucun modèle : ollama pull llama3.1:8b" }
  } else {
    Warn "Ollama non détecté (optionnel) : https://ollama.com puis « ollama pull llama3.1:8b »"
  }
  if (Test-Url "http://localhost:1234/v1/models") { Ok "LM Studio détecté" }
  $cloud = @("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MISTRAL_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY") |
    Where-Object { [Environment]::GetEnvironmentVariable($_) }
  if ($cloud) { Ok "fournisseurs cloud : $(($cloud | ForEach-Object { $_ -replace '_API_KEY', '' }) -join ', ')" }
  elseif (-not $ollamaUp) { Warn "aucun modèle disponible : le chat répondra « aucun modèle ». Ajoutez une clé dans .env ou installez Ollama." }

  # ---------- 5 bis. services locaux facultatifs (SearXNG, ComfyUI), dans ce même terminal ----------
  # SEARXNG_HOME / COMFYUI_HOME dans .env : dossier de leur installation (avec son venv). Relancés s'ils s'arrêtent.
  $companions = @()
  if ($env:SEARXNG_HOME -and (Test-Path "$env:SEARXNG_HOME\venv\Scripts\python.exe")) {
    $url = if ($env:SEARXNG_URL) { $env:SEARXNG_URL.TrimEnd("/") } else { "http://127.0.0.1:8080" }
    $companions += [pscustomobject]@{ Name = "SearXNG"; Log = "searxng"; Health = "$url/healthz"; Process = $null
      Exe = "$env:SEARXNG_HOME\venv\Scripts\python.exe"; Args = "-m searx.webapp"; Dir = "$env:SEARXNG_HOME\src"
      Env = @{ SEARXNG_SETTINGS_PATH = "$env:SEARXNG_HOME\settings.yml"; PYTHONPATH = "$env:SEARXNG_HOME\src" } }
  }
  if ($env:COMFYUI_HOME -and (Test-Path "$env:COMFYUI_HOME\venv\Scripts\python.exe")) {
    $url = if ($env:COMFYUI_URL) { $env:COMFYUI_URL.TrimEnd("/") } else { "http://127.0.0.1:8188" }
    $comfyPort = ([Uri]$url).Port
    $companions += [pscustomobject]@{ Name = "ComfyUI"; Log = "comfyui"; Health = "$url/system_stats"; Process = $null
      Exe = "$env:COMFYUI_HOME\venv\Scripts\python.exe"; Args = "main.py --listen 127.0.0.1 --port $comfyPort"
      Dir = $env:COMFYUI_HOME; Env = @{} }
  }
  function Start-Companion($c) {
    # variables propres à ce service seulement : PYTHONPATH de SearXNG ne doit pas passer à la gateway
    $saved = @{}
    foreach ($k in $c.Env.Keys) {
      $saved[$k] = [Environment]::GetEnvironmentVariable($k, "Process")
      [Environment]::SetEnvironmentVariable($k, $c.Env[$k], "Process")
    }
    try {
      $c.Process = Start-Process $c.Exe -ArgumentList $c.Args -WorkingDirectory $c.Dir -NoNewWindow -PassThru `
        -RedirectStandardOutput "$logs\$($c.Log).out.log" -RedirectStandardError "$logs\$($c.Log).log"
      $null = $c.Process.Handle                     # sinon ExitCode reste vide
      [void]$started.Add($c.Process)
      Save-ServiceState
    } finally {
      foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], "Process") }
    }
  }
  function Watch-Companions {
    foreach ($c in $companions) {
      if ($c.Process -and $c.Process.HasExited) {
        Warn "$($c.Name) s'est arrêté (code $($c.Process.ExitCode)) : relance (journal : data\logs\$($c.Log).log)"
        $started.Remove($c.Process)
        Start-Companion $c
      }
    }
  }
  # tâche quotidienne facultative (DAILY_SCRIPT dans .env, ex. sauvegarde des documents) : une fois par 24 h, dans
  # sa propre console cachée (elle finit même si ce terminal est fermé) ; en échec, nouvel essai 1 h plus tard
  $dailyStamp = Join-Path $PSScriptRoot "data\daily-last.txt"
  $script:daily, $script:dailyRetry = $null, [datetime]::MinValue
  function Start-Daily {
    if (-not $env:DAILY_SCRIPT -or -not (Test-Path $env:DAILY_SCRIPT)) { return }
    if ($script:daily) {
      if (-not $script:daily.HasExited) { return }
      if ($script:daily.ExitCode -eq 0) { (Get-Date).ToString("o") | Out-File $dailyStamp -Encoding ascii }
      else {
        Warn "tâche quotidienne en échec (code $($script:daily.ExitCode)) : nouvel essai dans 1 h (data\logs\daily.log)"
        $script:dailyRetry = (Get-Date).AddHours(1)
      }
      $script:daily = $null
      return
    }
    if ((Get-Date) -lt $script:dailyRetry) { return }
    if (Test-Path $dailyStamp) {
      $last = [datetime]::Parse((Get-Content $dailyStamp -Raw).Trim(), $null, "RoundtripKind")
      if (((Get-Date) - $last).TotalHours -lt 24) { return }
    }
    $script:daily = Start-Process powershell.exe -WindowStyle Hidden -PassThru `
      -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$env:DAILY_SCRIPT`"" `
      -RedirectStandardOutput "$logs\daily.out.log" -RedirectStandardError "$logs\daily.log"
    $null = $script:daily.Handle                     # sinon ExitCode reste vide
    Ok "tâche quotidienne lancée : $env:DAILY_SCRIPT"
  }
  if ($companions) {
    Step "Services locaux"
    foreach ($c in $companions) {
      if (Test-Url $c.Health 3) { Ok "$($c.Name) tourne déjà" }
      else { Start-Companion $c; Ok "$($c.Name) lancé (journal : data\logs\$($c.Log).log)" }
    }
  }

  # ---------- 6. gateway (relancée si elle s'arrête : 18.1) ----------
  Step "Gateway"
  if ($Https) {
    # certificat signé par l'autorité locale de ce PC (data\tls), refait si les adresses réseau changent
    $tlsInfo = & python -m gateway.tls
    if ($LASTEXITCODE -ne 0) { Fail "création du certificat HTTPS impossible." }
    $fingerprint = ($tlsInfo | Select-String "SHA-256 : (.+)$").Matches[0].Groups[1].Value
    Ok "HTTPS réseau local (port $TlsPort) — empreinte de l'autorité : $fingerprint"
  }
  # superviseur présent : la gateway peut demander à être relancée (code 3, POST /v1/admin/restart)
  $env:GATEWAY_SUPERVISED = "1"
  $first, $delay = $true, 5
  while ($true) {
    # HTTP sur 127.0.0.1 (ce PC), plus HTTPS sur le réseau avec -Https, dans un seul processus
    $gwArgs = @{ FilePath = "python"; ArgumentList = "-m gateway.serve --port $Port"; PassThru = $true }
    if ($Service) {
      # journal de l'exécution précédente gardé (.1) : celui d'un arrêt inattendu
      foreach ($f in "gateway.log", "gateway.out.log") {
        if (Test-Path "$logs\$f") { Move-Item "$logs\$f" "$logs\$f.1" -Force -ErrorAction SilentlyContinue }
      }
      $gw = Start-Process @gwArgs -NoNewWindow -RedirectStandardOutput "$logs\gateway.out.log" `
        -RedirectStandardError "$logs\gateway.log"
    } else {
      $gw = Start-Process @gwArgs -NoNewWindow
    }
    $null = $gw.Handle                                 # sinon ExitCode reste vide une fois le processus terminé
    [void]$started.Add($gw)
    Save-ServiceState
    $since = Get-Date
    $up = $false
    for ($i = 0; $i -lt 60 -and -not $up -and -not $gw.HasExited; $i++) {
      Start-Sleep 1
      $up = Test-Url "http://127.0.0.1:$Port/health"
    }
    if (-not $up -and -not $Service) {
      if ($gw.HasExited) { Fail "la gateway s'est arrêtée au démarrage (voir les messages ci-dessus)." }
      Fail "la gateway ne répond pas après 60 s."
    }
    if ($up) {
      Ok "gateway prête sur http://localhost:$Port"
      if ($first) {
        $first = $false
        # ---------- 7. agent de pont ----------
        if ($env:GATEWAY_BRIDGE_TOKEN) {
          Step "Pont vers les modèles locaux"
          $target = if ($env:GATEWAY_URL) { $env:GATEWAY_URL } else { "http://127.0.0.1:$Port" }
          $p = Start-Process python -ArgumentList "-m gateway.bridge_agent --gateway $target" -NoNewWindow -PassThru `
            -RedirectStandardOutput "$logs\bridge.out.log" -RedirectStandardError "$logs\bridge.log"
          [void]$started.Add($p)
          Save-ServiceState
          Ok "agent de pont lancé vers $target (journal : data\logs\bridge.log)"
        }

        # ---------- 8. récapitulatif ----------
        Write-Host ""
        Write-Host "  ============================================================" -ForegroundColor White
        Write-Host "   Interface      : http://localhost:$Port"
        foreach ($u in $lanUrls) { Write-Host "   Réseau local   : $u  (clé requise)" }
        if ($Https -and $lanUrls) {
          Write-Host "   Certificat     : $($lanUrls[0])/ca.crt  à installer une fois sur chaque appareil"
          Write-Host "                    empreinte SHA-256 à vérifier : $fingerprint"
        }
        # secrets jamais affichés (9.6) : la console finit dans des captures d'écran, des journaux, des partages d'écran
        Write-Host "   Admin          : clé maître = GATEWAY_MASTER_KEY dans $envFile"
        Write-Host "   Compte de test : jetons GATEWAY_KEYS du même fichier (plan Free)"
        Write-Host "   Mode Test      : bouton « Essayer sans clé » sur la page de connexion"
        Write-Host "   API            : http://localhost:$Port/docs  (depuis ce PC uniquement)"
        Write-Host "   Arrêt          : $(if ($Service) { '.\lancer.ps1 -Stop' } else { 'Ctrl+C' })"
        Write-Host "  ============================================================" -ForegroundColor White
        if (-not $NoBrowser) { Start-Process "http://localhost:$Port" }
      }
      for ($tick = 0; -not $gw.HasExited; $tick++) {
        Start-Sleep 1
        if ($tick % 30 -eq 29) { Watch-Companions; Start-Daily }   # services relancés ; tâche du jour
      }
    } elseif (-not $gw.HasExited) {
      Warn "la gateway ne répond pas après 60 s : arrêt forcé"
      Stop-Process -Id $gw.Id -Force -ErrorAction SilentlyContinue
      $gw.WaitForExit()
    }
    $started.Remove($gw)
    $code = $gw.ExitCode
    if ($code -eq 3) { Ok "redémarrage demandé : relance de la gateway"; continue }
    if (-not $Service) { Warn "la gateway s'est arrêtée."; break }
    # arrêt inattendu : attente croissante (5 s, 10 s… 5 min), remise à 5 s après 5 min de fonctionnement
    $delay = if (((Get-Date) - $since).TotalMinutes -ge 5) { 5 } else { [Math]::Min($delay * 2, 300) }
    Warn "la gateway s'est arrêtée (code $code) : relance dans $delay s (journal : data\logs\gateway.log.1)"
    Start-Sleep $delay
  }
}
finally {
  Write-Host "`n  Arrêt des processus lancés par le script..."
  foreach ($p in $started) {
    if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
  }
  if ($Service) { Remove-Item $stateFile -Force -ErrorAction SilentlyContinue; Stop-Transcript | Out-Null }
  Write-Host "  Terminé."
}
