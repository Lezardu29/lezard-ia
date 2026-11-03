# Lezard IA pour Android

Application Android qui ouvre l'interface web de Lezard IA (`gateway/web`) du PC, dans une WebView. Rien ne tourne
sur le téléphone : les modèles restent sur le PC, l'application en est la fenêtre.

## Ce qu'elle ajoute au navigateur

- **Scanner le QR code** du PC : dans le chat de Lezard IA sur le PC, bouton « Connecter un téléphone »
  (`static/phone-panel.js`) ; le QR porte l'adresse et la clé de la session du PC (mêmes conversations). Caméra en
  direct (Camera2, `ScanActivity`), décodage ZXing (`QrDecoder`), puis même vérification qu'une saisie.
- **Écran de connexion** : ou bien on colle une clé web `lz.<nom>.<clé>` (Pro, ouvre `https://<nom>.lezard-ia.fr`), un lien
  d'accès `https://…/#key=…` (QR code d'une clé, 12.6) ou l'adresse du PC (`https://…`, `192.168.1.20:8000`).
  L'adresse est vérifiée (`/health`) avant d'être retenue. « Changer de serveur » sur la page de connexion du PC
  et sur la page hors connexion (`static/offline.js`).
- **Micro** de la dictée (permission demandée au premier appui), **appareil photo** et fichiers pour les pièces jointes.
- **Enregistrement des fichiers** produits par la page (exports, clé de chiffrement) : `static/save-file.js` passe
  par le pont `window.LezardApp` au lieu d'un lien `blob:`, que la WebView ignore.
- **Copie** dans le presse-papiers, y compris en HTTP sur le réseau local.
- Liens externes ouverts dans le navigateur ; écran « PC injoignable » avec Réessayer / Changer d'adresse.

La clé d'accès reste dans le stockage de la page (comme dans un navigateur) ; la sauvegarde Android est désactivée.

## Construire

Aucune dépendance Gradle : seulement le SDK Android (`platforms/android-35`, un `build-tools`) et un JDK 11 ou plus.
Seule bibliothèque : ZXing (`com.google.zxing:core` 3.5.3, Apache 2.0), téléchargée une fois dans `android/libs/` et
refusée si son empreinte SHA-256 diffère ; sa licence est livrée dans l'application (`assets/licences.txt`).
Avant d'emballer, `tests/QrDecoderCheck.java` vérifie la lecture des QR codes sur le PC (tournés, petits, flous,
peu contrastés, bruités) ; on peut lui passer des captures réelles :
`java -cp "android/build/check;android/libs/zxing-core-3.5.3.jar" fr.lezardia.app.QrDecoderCheck capture.png`.

```
python android/build.py              # -> android/build/lezard-ia.apk
python android/build.py --install    # puis installation sur le téléphone branché en USB (débogage USB activé)
```

La première construction crée la clé de signature `data/android/lezard-ia.jks` et son mot de passe
`data/android/keystore.properties` (hors dépôt). **À sauvegarder** : une mise à jour doit être signée par la même clé,
sinon Android refuse de l'installer par-dessus l'ancienne.

Nouvelle version : augmenter `android:versionCode` (et `versionName`) dans `AndroidManifest.xml`.

## Installer sur un téléphone sans câble

Copier `lezard-ia.apk` sur le téléphone (Drive, e-mail…), l'ouvrir, et autoriser l'installation depuis cette source
quand Android le demande.
