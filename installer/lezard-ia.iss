; Installateur Windows de Lezard IA (32.1) — compilé par installer\build.ps1, qui prépare build\stage :
;   python\     Python embarqué + dépendances (requirements.lock)
;   gateway\    code de l'application (commit construit)
;   lezard.py   lanceur : configuration, superviseur, ouverture du navigateur, arrêt
;
; Installation par utilisateur, sans droits administrateur (dossier %LOCALAPPDATA%\Programs\Lezard IA) : la gateway
; tourne sous le compte du client, comme Ollama. Démarrage automatique par un raccourci du dossier Démarrage (aucune
; tâche planifiée). Aucun modèle n'est installé. La désinstallation garde data\ et .env, sauf demande du client.
;
; Options de la ligne de commande, en plus de celles d'Inno Setup (/SILENT, /DIR=…, /MERGETASKS=…) :
;   /PORT=8080       port de l'interface au premier passage (défaut : premier port libre à partir de 8000)
;   désinstallation silencieuse : /DELETEDATA=1 supprime aussi les données (sinon gardées)

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Commit
  #define Commit "dev"
#endif
#ifndef StageDir
  #define StageDir "build\stage"
#endif
; tailles à télécharger des composants facultatifs (build.ps1, d'après installer\packs\*.json)
#ifndef VoixMo
  #define VoixMo "?"
#endif
#ifndef MultiMo
  #define MultiMo "?"
#endif

[Setup]
AppId={{6E0C5A3D-7B1F-4C8E-9A42-1D5F3B7C9E20}
AppName=Lezard IA
AppVersion={#AppVersion}
AppVerName=Lezard IA {#AppVersion}
AppPublisher=Lezard IA
AppPublisherURL=https://lezard-ia.fr
AppSupportURL=https://lezard-ia.fr
VersionInfoVersion={#AppVersion}
VersionInfoDescription=Installateur de Lezard IA (commit {#Commit})
DefaultDirName={localappdata}\Programs\Lezard IA
DefaultGroupName=Lezard IA
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputBaseFilename=LezardIA-Setup-{#AppVersion}
SetupIconFile={#StageDir}\lezard-ia.ico
UninstallDisplayIcon={app}\lezard-ia.ico
UninstallDisplayName=Lezard IA
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; le lanceur arrête lui-même le service avant une mise à jour (PrepareToInstall)
CloseApplications=no
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "autostart"; Description: "Démarrer Lezard IA avec Windows (recommandé)"
Name: "lan"; Description: "Utiliser Lezard IA depuis les appareils de la maison (téléphone, tablette), en HTTPS"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"
; seul ce qui touche au web se choisit ici. Les autres outils (contrôle du PC, dictée et lecture vocales,
; multi-agents, Ollama et les modèles) s'installent après, en deux clics, depuis la marketplace de Lezard IA.
Name: "web"; Description: "Recherche sur Internet (commande /web)"; GroupDescription: "Utilisation du web :"

[InstallDelete]
; ancien dossier des fonctions Pro (avant 1.0.7) : elles font maintenant partie de gateway\
Type: filesandordirs; Name: "{app}\lezard_pro"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; arrêt du service avant une mise à jour, sans arrêter l'installateur (PrepareToInstall) ; jamais copié
Source: "arret-maj.py"; Flags: dontcopy

[Icons]
Name: "{group}\Lezard IA"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\lezard.py"" open"; WorkingDir: "{app}"; IconFilename: "{app}\lezard-ia.ico"; Comment: "Ouvrir Lezard IA dans le navigateur"
Name: "{group}\Copier ma clé d'administration"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\lezard.py"" key"; WorkingDir: "{app}"; IconFilename: "{app}\lezard-ia.ico"; Comment: "Copie la clé demandée par la page de connexion"
Name: "{group}\Désinstaller Lezard IA"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Lezard IA"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\lezard.py"" open"; WorkingDir: "{app}"; IconFilename: "{app}\lezard-ia.ico"; Tasks: desktopicon
; démarrage automatique : 30 s après l'ouverture de session, le temps que le réseau et Ollama démarrent
Name: "{userstartup}\Lezard IA"; Filename: "{app}\python\python.exe"; Parameters: """{app}\lezard.py"" service --delay 30"; WorkingDir: "{app}"; IconFilename: "{app}\lezard-ia.ico"; Tasks: autostart; Flags: runminimized

[Registry]
; installation faite avant depuis le zip (lezard.py register) : son entrée « Applications installées » est remplacée
; par celle de l'installateur
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Uninstall\LezardIA-zip"; Flags: deletekey dontcreatekey

[Run]
; .env au premier passage : clé d'administration, clé de chiffrement, port (jamais écrasé)
Filename: "{app}\python\python.exe"; Parameters: """{app}\lezard.py"" setup --port {param:PORT|0} --lan"; WorkingDir: "{app}"; StatusMsg: "Préparation de la configuration..."; Tasks: lan; Flags: runhidden waituntilterminated
Filename: "{app}\python\python.exe"; Parameters: """{app}\lezard.py"" setup --port {param:PORT|0}"; WorkingDir: "{app}"; StatusMsg: "Préparation de la configuration..."; Tasks: not lan; Flags: runhidden waituntilterminated
; pare-feu : réseaux PRIVÉS seulement (maison), jamais un Wi-Fi public ; demande l'accord de l'administrateur (UAC)
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Lezard IA"" dir=in action=allow program=""{app}\python\python.exe"" profile=private"; Verb: runas; StatusMsg: "Ouverture au réseau de la maison..."; Tasks: lan; Flags: shellexec runhidden waituntilterminated
; composants cochés, puis mise à jour de ceux déjà installés (mise à jour de Lezard IA), avant le démarrage du service
Filename: "{app}\python\python.exe"; Parameters: """{app}\lezard.py"" tools {code:ToolChoices}"; WorkingDir: "{app}"; StatusMsg: "Activation des outils choisis..."; Flags: runhidden waituntilterminated
Filename: "{app}\python\python.exe"; Parameters: """{app}\lezard.py"" pack refresh"; WorkingDir: "{app}"; StatusMsg: "Mise à jour des composants..."; Flags: runhidden waituntilterminated
Filename: "{app}\python\python.exe"; Parameters: """{app}\lezard.py"" service"; WorkingDir: "{app}"; Tasks: autostart; Flags: runminimized nowait
; mise à jour lancée depuis le tableau de bord (32.3, mode silencieux) : Lezard IA redémarre même sans « autostart »
Filename: "{app}\python\python.exe"; Parameters: """{app}\lezard.py"" service"; WorkingDir: "{app}"; Check: WizardSilent and not WizardIsTaskSelected('autostart'); Flags: runminimized nowait
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\lezard.py"" open"; WorkingDir: "{app}"; Description: "Ouvrir Lezard IA maintenant"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Lezard IA"""; Verb: runas; RunOnceId: "FirewallLezard"; Flags: shellexec runhidden waituntilterminated
Filename: "{app}\python\python.exe"; Parameters: """{app}\lezard.py"" stop"; WorkingDir: "{app}"; RunOnceId: "StopLezard"; Flags: runhidden waituntilterminated

[UninstallDelete]
; fichiers créés à l'exécution (cache Python, état du service) ; data\ et .env restent, sauf demande du client
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\gateway"
Type: filesandordirs; Name: "{app}\packs"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files; Name: "{app}\data\launcher.*"
; fichiers d'une installation faite avant depuis le zip
Type: files; Name: "{app}\mise_a_jour.py"
Type: files; Name: "{app}\arret-maj.py"
Type: files; Name: "{app}\version.txt"
Type: files; Name: "{app}\Installer Lezard IA.cmd"

[Code]
{ message en cas d'échec d'un composant : installation interactive seulement (une mise à jour silencieuse ne doit
  jamais attendre un clic) }
{ outils cochés, écrits dans .env par lezard.py tools (à chaque installation et mise à jour) }
function OnOff(Task: String): String;
begin
  if WizardIsTaskSelected(Task) then
    Result := 'on'
  else
    Result := 'off';
end;

function ToolChoices(Param: String): String;
begin
  Result := 'web=' + OnOff('web');   { contrôle du PC : depuis la marketplace, jamais changé par une mise à jour }
end;

{ mise à jour : le service en cours tient des fichiers ouverts (python\) ; il est arrêté avant la copie.
  Pas « lezard.py stop » : il arrête la gateway avec tout son arbre (taskkill /T), et une mise à jour lancée depuis
  le tableau de bord descend de la gateway : elle était arrêtée avec elle, sans rien installer ni relancer Lezard IA.
  arret-maj.py (fourni par CET installateur, valable quelle que soit la version installée) arrête le service puis la
  gateway, chacun seul : l'installation continue et relance Lezard IA à la fin. }
{ filet de sécurité : si la copie échoue après l'arrêt du service (fichier resté ouvert, disque plein…), Lezard IA est
  relancé tel qu'il est plutôt que laissé arrêté sans que personne le sache (1.0.23). }
var
  StoppedApp: String;
  Installed: Boolean;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    Installed := True;
end;

procedure DeinitializeSetup();
var
  Code: Integer;
begin
  if (StoppedApp <> '') and not Installed then
    if FileExists(StoppedApp + '\lezard.py') and FileExists(StoppedApp + '\python\python.exe') then begin
      Log('Installation interrompue : Lezard IA est relancé.');
      Exec(StoppedApp + '\python\python.exe', '"' + StoppedApp + '\lezard.py" service', StoppedApp,
           SW_SHOWMINNOACTIVE, ewNoWait, Code);
    end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  App: String;
  Code: Integer;
begin
  Result := '';
  App := ExpandConstant('{app}');
  if FileExists(App + '\lezard.py') and FileExists(App + '\python\python.exe') then begin
    StoppedApp := App;
    ExtractTemporaryFile('arret-maj.py');
    Exec(App + '\python\python.exe', '"' + ExpandConstant('{tmp}\arret-maj.py') + '" "' + App + '"', App, SW_HIDE,
         ewWaitUntilTerminated, Code);
    Log('Arrêt de Lezard IA pour la mise à jour : code ' + IntToStr(Code));
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Info: String;
begin
  if CurPageID = wpFinished then begin
    Info := 'Aucune IA locale n''est installée. Pour en choisir : page Modèles, « Tester les performances de ce PC ». ' +
            'Le test indique la vitesse de chaque modèle sur votre PC ; vous installez ensuite ceux que vous voulez ' +
            '(Ollama compris, s''il le faut). Vous pouvez aussi utiliser des IA en ligne avec une clé.';
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 + Info;
    WizardForm.AdjustLabelHeight(WizardForm.FinishedLabel);
    WizardForm.RunList.Top := WizardForm.FinishedLabel.Top + WizardForm.FinishedLabel.Height + ScaleY(12);
  end;
end;

{ données du client (data\, .env : clés et chiffrement) gardées par défaut ; supprimées seulement à sa demande }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  App: String;
  Delete: Boolean;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  App := ExpandConstant('{app}');
  if not (DirExists(App + '\data') or FileExists(App + '\.env')) then
    Exit;
  if UninstallSilent then
    Delete := ExpandConstant('{param:DELETEDATA|0}') = '1'
  else
    Delete := MsgBox('Supprimer aussi vos données ?' + #13#10#13#10 +
                     'Conversations, documents, réglages et clés de Lezard IA (' + App + ').' + #13#10#13#10 +
                     'Choisissez Non pour les garder : une prochaine installation les retrouvera.',
                     mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  if Delete then
    DelTree(App, True, True, True);
end;
