<#
  Construit l'installateur Windows de Lezard IA (32.1) : installer\build\out\LezardIA-Setup-<version>.exe,
  et le zip (installation sans installateur, installer\zip.py) : installer\build\out\LezardIA-<version>.zip

    .\installer\build.ps1              construction
    .\installer\build.ps1 -Test        construction, puis suite de tests du projet avec les dépendances embarquées
    .\installer\build.ps1 -UpdateLock  regénère installer\requirements.lock et les composants (installer\packs\*.json)

  Reproductible : mêmes entrées pour un même commit.
    - Python embarqué et Inno Setup : versions figées ci-dessous, empreintes SHA-256 vérifiées (téléchargés une fois
      dans installer\build\cache) ; signature Authenticode de python.exe vérifiée en plus.
    - Dépendances : requirements.lock (versions exactes + empreintes, pip --require-hashes).
    - Code : le commit courant (git archive HEAD gateway), jamais les fichiers en cours de modification.
  Prérequis du PC de construction : git, et un Python 3.10+ avec pip (il ne sert qu'à télécharger les roues).
  Inno Setup est installé en mode portable dans installer\build\tools si ISCC.exe est introuvable.
  Signature de code de l'installateur : plus tard (32.2), le certificat n'est pas acheté.

  Rien n'est installé ni lancé sur ce PC : pour essayer l'installateur, voir installer\README.md.
#>
param([switch]$Test, [switch]$UpdateLock)

# erreurs des commandes PowerShell : bloquantes ; programmes externes (pip, git) : code de sortie vérifié
$PSDefaultParameterValues["*:ErrorAction"] = "Stop"
$here = $PSScriptRoot
$root = Split-Path $here -Parent
$build = Join-Path $here "build"
$cache = Join-Path $build "cache"
$stage = Join-Path $build "stage"
$out = Join-Path $build "out"

# ---------- entrées figées ----------
$PythonVersion = "3.13.17"
$PythonZip = "python-$PythonVersion-embed-amd64.zip"
$PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/$PythonZip"
$PythonSha = "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"
$InnoVersion = "6.7.3"
$InnoExe = "innosetup-$InnoVersion.exe"
$InnoUrl = "https://github.com/jrsoftware/issrc/releases/download/is-$($InnoVersion -replace '\.', '_')/$InnoExe"
$InnoSha = "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732"

function Step($m) { Write-Host "`n> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  [x]  $m" -ForegroundColor Red; exit 1 }

function Get-Pinned($name, $url, $sha) {
  $path = Join-Path $cache $name
  if (-not (Test-Path $path)) {
    Write-Host "  téléchargement de $name..."
    New-Item -ItemType Directory -Force $cache | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -UseBasicParsing $url -OutFile "$path.part"
    Move-Item "$path.part" $path -Force
  }
  $actual = (Get-FileHash $path -Algorithm SHA256).Hash.ToLower()
  if ($actual -ne $sha) {
    Remove-Item $path -Force
    Fail "$name : empreinte inattendue ($actual) ; fichier supprimé. Attendu : $sha"
  }
  Ok "$name (SHA-256 vérifié)"
  return $path
}

$hostPython = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $hostPython) { Fail "Python introuvable : il sert à télécharger les dépendances (https://www.python.org/downloads/)." }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "git introuvable." }

if ($UpdateLock) {
  Step "Dépendances figées"
  & $hostPython (Join-Path $here "lock.py")
  if ($LASTEXITCODE -ne 0) { Fail "résolution des dépendances impossible." }
  Ok "installer\requirements.lock et installer\packs\*.json mis à jour : vérifiez avec -Test avant de versionner"
}

# ---------- version et commit ----------
Step "Version"
Push-Location $root
try {
  $commit = (git rev-parse --short HEAD).Trim()
  $version = ((Get-Content "gateway\__init__.py" -Encoding utf8) | Select-String '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
  if (git status --porcelain -- gateway) { Warn "gateway\ a des modifications non validées : elles ne sont PAS embarquées (commit $commit)" }
} finally { Pop-Location }
Ok "Lezard IA $version (commit $commit)"

# ---------- Python embarqué ----------
Step "Python embarqué $PythonVersion"
$zip = Get-Pinned $PythonZip $PythonUrl $PythonSha
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
$py = Join-Path $stage "python"
Expand-Archive $zip -DestinationPath $py
$sig = Get-AuthenticodeSignature (Join-Path $py "python.exe")
if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notlike "*Python Software Foundation*") {
  Fail "python.exe : signature invalide ($($sig.Status), $($sig.SignerCertificate.Subject))"
}
Ok "python.exe signé par la Python Software Foundation"
# chemins de recherche : bibliothèque standard, dépendances, dossier d'installation (paquet gateway, lezard.py) ;
# « import site » pour les fichiers .pth des dépendances. Les variables PYTHON* du PC sont ignorées (fichier ._pth).
$pth = Get-ChildItem $py -Filter "python3*._pth" | Select-Object -First 1
$stdlib = Get-ChildItem $py -Filter "python3*.zip" | Select-Object -First 1
@($stdlib.Name, ".", "Lib\site-packages", "..", "import site") | Set-Content $pth.FullName -Encoding ascii

# ---------- dépendances ----------
Step "Dépendances (requirements.lock)"
$site = Join-Path $py "Lib\site-packages"
& $hostPython -m pip install --disable-pip-version-check --quiet --no-deps --require-hashes --no-compile `
  --only-binary=:all: --platform win_amd64 --python-version 3.13 --implementation cp `
  --target $site -r (Join-Path $here "requirements.lock")
if ($LASTEXITCODE -ne 0) { Fail "installation des dépendances impossible." }
Get-ChildItem $site -Directory -Filter "bin" | Remove-Item -Recurse -Force    # scripts pip inutilisables ici
# packs facultatifs (packs\<pack>, installés par lezard.py seulement s'ils sont choisis) : ajoutés au chemin de Python
# s'ils existent, leurs propres fichiers .pth compris (site.addsitedir)
'import os, site, sys; r = os.path.join(os.path.dirname(sys.prefix), "packs"); [site.addsitedir(os.path.join(r, p)) for p in (sorted(os.listdir(r)) if os.path.isdir(r) else []) if os.path.isdir(os.path.join(r, p)) and not p.startswith(".")]' |
  Set-Content (Join-Path $site "lezard-packs.pth") -Encoding ascii
Ok "$((Get-ChildItem $site -Directory -Filter '*.dist-info').Count) paquets"

# ---------- code de Lezard IA ----------
Step "Code (commit $commit)"
$archive = Join-Path $build "gateway.zip"
git -C $root archive --format=zip -o $archive HEAD gateway
if ($LASTEXITCODE -ne 0) { Fail "git archive impossible." }
Expand-Archive $archive -DestinationPath $stage
Remove-Item $archive
Copy-Item (Join-Path $here "app\lezard.py") $stage
# manifestes des composants facultatifs (installer\lock.py) : rien n'est téléchargé tant que le client ne choisit pas
New-Item -ItemType Directory -Force (Join-Path $stage "packs") | Out-Null
Copy-Item (Join-Path $here "packs\*.json") (Join-Path $stage "packs")
$packMo = @{}
foreach ($p in "voix", "multi") {
  $m = Get-Content (Join-Path $here "packs\$p.json") -Raw -Encoding utf8 | ConvertFrom-Json
  $packMo[$p] = [Math]::Round(($m.wheels | Measure-Object size -Sum).Sum / 1MB)
}
$ollama = Get-Content (Join-Path $here "packs\ollama.json") -Raw -Encoding utf8 | ConvertFrom-Json
Ok "gateway\, lezard.py ; composants : voix $($packMo.voix) Mo, multi $($packMo.multi) Mo, Ollama $($ollama.version) $([Math]::Round($ollama.size / 1MB)) Mo"

$pyexe = Join-Path $py "python.exe"
Push-Location $stage
try {
  & $pyexe -c "import gateway.main, lezard"                    # tout s'importe avec le Python embarqué seul
  if ($LASTEXITCODE -ne 0) { Fail "le code ne s'importe pas avec le Python embarqué (voir ci-dessus)." }
  # icône des raccourcis et de l'installateur, depuis le logo de l'application (Pillow vient avec fpdf2)
  & $pyexe -c "from PIL import Image; Image.open(r'gateway\web\static\icons\icon-512.png').save(r'lezard-ia.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
  if ($LASTEXITCODE -ne 0) { Fail "icône impossible à créer." }
  # code livré sans ses sources (33.1) : bytecode optimisé (ni commentaires, ni docstrings, ni assertions) à la
  # place de chaque .py ; le lanceur lezard.py et les scripts lancés tels quels (.pyw, .ps1) restent en clair
  & $pyexe -m compileall -q -b -o 2 -j 0 gateway | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail "compilation du code impossible." }
  Get-ChildItem gateway -Recurse -File -Filter "*.py" | Remove-Item -Force
  Get-ChildItem gateway -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
  & $pyexe -c "import gateway.main, gateway.tunnel, gateway.tools.web_tools"   # sans sources, complet
  if ($LASTEXITCODE -ne 0) { Fail "le code compilé ne s'importe pas (voir ci-dessus)." }
  # caches laissés par les vérifications d'import ci-dessus : ils gardent le chemin absolu de ce PC de construction.
  # Effacés, puis tout est recompilé en chemins relatifs (rien du PC de construction dans l'installateur)
  Get-ChildItem -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
  & $pyexe -m compileall -q -j 0 python lezard.py | Out-Null # premier démarrage plus rapide
  $leak = Get-ChildItem -Recurse -File -Filter "*.pyc" | Where-Object { [IO.File]::ReadAllText($_.FullName).Contains($stage) }
  if ($leak) { Fail "chemin du PC de construction dans $($leak.Count) fichier(s) compilé(s), par exemple $($leak[0].FullName)" }
} finally { Pop-Location }
Ok "code compilé sans sources, toutes les fonctions incluses, vérifié avec le Python embarqué"

if ($Test) {
  Step "Tests du projet avec les dépendances embarquées"
  # Python du PC (même version mineure : 3.13), dépendances embarquées en tête du chemin ; outils de test du PC
  & $hostPython -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)"
  if ($LASTEXITCODE -ne 0) { Fail "-Test demande Python 3.13 sur ce PC (version des dépendances embarquées)." }
  Push-Location $root
  try {
    & $hostPython -c "import sys; sys.path.insert(0, sys.argv[1]); import pytest; sys.exit(pytest.main(['-q', '-x', '-p', 'no:cacheprovider']))" $site
    if ($LASTEXITCODE -ne 0) { Fail "des tests échouent avec les dépendances embarquées : installateur non construit." }
  } finally { Pop-Location }
  Ok "tous les tests passent"
}

# ---------- Inno Setup ----------
Step "Inno Setup $InnoVersion"
$iscc = @((Join-Path $build "tools\inno\ISCC.exe"),
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
  $setup = Get-Pinned $InnoExe $InnoUrl $InnoSha
  $dir = Join-Path $build "tools\inno"
  # mode portable : rien dans le registre ni dans le menu Démarrer, tout reste dans installer\build\tools
  $p = Start-Process $setup -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
    "/PORTABLE=1", "/NOICONS", "/DIR=`"$dir`"" -Wait -PassThru
  $iscc = Join-Path $dir "ISCC.exe"
  if ($p.ExitCode -ne 0 -or -not (Test-Path $iscc)) { Fail "installation portable d'Inno Setup impossible (code $($p.ExitCode))." }
}
Ok $iscc

Step "Installateur"
& $iscc /Q "/DAppVersion=$version" "/DCommit=$commit" "/DStageDir=$stage" "/O$out" `
  "/DVoixMo=$($packMo.voix)" "/DMultiMo=$($packMo.multi)" `
  (Join-Path $here "lezard-ia.iss")
if ($LASTEXITCODE -ne 0) { Fail "compilation Inno Setup impossible (voir ci-dessus)." }
$exe = Join-Path $out "LezardIA-Setup-$version.exe"
$size = [Math]::Round((Get-Item $exe).Length / 1MB, 1)
Ok "$exe ($size Mo)"
Write-Host "  SHA-256 : $((Get-FileHash $exe -Algorithm SHA256).Hash.ToLower())"
Warn "non signé (32.2) : Windows SmartScreen affichera « éditeur inconnu »"

# zip : même programme, installé par « Installer Lezard IA.cmd » et mis à jour sans installateur (mise_a_jour.py)
Step "Zip"
$zip = & $hostPython (Join-Path $here "zip.py") $stage $version $out
if ($LASTEXITCODE -ne 0) { Fail "zip impossible (voir ci-dessus)." }
Ok "$zip ($([Math]::Round((Get-Item $zip).Length / 1MB, 1)) Mo)"
Write-Host "  SHA-256 : $((Get-FileHash $zip -Algorithm SHA256).Hash.ToLower())"
