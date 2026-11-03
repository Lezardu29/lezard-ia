"""Construit l'application Android (android/) sans Gradle, avec les outils du SDK Android : aapt2 (ressources),
javac (JDK 11 ou plus), d8 (dex), zipalign et apksigner.

    python android/build.py            ->  android/build/lezard-ia.apk (signée)
    python android/build.py --install  ->  idem, puis installation sur le téléphone branché (adb, débogage USB)

SDK : ANDROID_HOME, ANDROID_SDK_ROOT ou %LOCALAPPDATA%\\Android\\Sdk ; il faut platforms/android-35 et un build-tools.
Bibliothèque : ZXing (lecture des QR codes, Apache 2.0), téléchargée une fois depuis Maven Central dans android/libs/
et vérifiée par son empreinte SHA-256 ; sa licence est dans assets/licences.txt.
Clé de signature : data/android/lezard-ia.jks, créée à la première construction (mot de passe aléatoire dans
data/android/keystore.properties, data/ n'est jamais versionné). À GARDER : une mise à jour de l'application doit être
signée par la même clé, sinon le téléphone refuse de l'installer par-dessus.
"""

import hashlib
import os
import secrets
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUILD = HERE / "build"
KEY_DIR = ROOT / "data" / "android"
KEYSTORE = KEY_DIR / "lezard-ia.jks"
KEY_PROPS = KEY_DIR / "keystore.properties"
ALIAS = "lezard-ia"
PLATFORM = "android-35"
MIN_API = "26"
LIBS = HERE / "libs"
# (fichier, adresse, SHA-256) : une bibliothèque modifiée ou corrompue arrête la construction
DEPENDENCIES = [
    (
        "zxing-core-3.5.3.jar",
        "https://repo1.maven.org/maven2/com/google/zxing/core/3.5.3/core-3.5.3.jar",
        "8d8064c1636fdaef7189dd9055c7d59950a8940a12f2293956446ec3c109fd82",
    ),
]


def sdk() -> Path:
    for candidate in (
        os.getenv("ANDROID_HOME"),
        os.getenv("ANDROID_SDK_ROOT"),
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Android", "Sdk"),
    ):
        if candidate and (Path(candidate) / "platforms" / PLATFORM / "android.jar").is_file():
            return Path(candidate)
    sys.exit(f"SDK Android introuvable (platforms/{PLATFORM}) : définissez ANDROID_HOME.")


def jdk_tool(name: str) -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    java_home = os.getenv("JAVA_HOME")
    if java_home and (Path(java_home) / "bin" / exe).is_file():
        return str(Path(java_home) / "bin" / exe)
    found = shutil.which(name)
    if not found and os.name == "nt":  # le PATH n'a souvent que java.exe (javapath d'Oracle)
        found = next((str(p) for p in sorted(Path(r"C:\Program Files\Java").glob(f"jdk*/bin/{exe}"))), None)
    if not found:
        sys.exit(f"{name} introuvable : installez un JDK (11 ou plus) ou définissez JAVA_HOME.")
    return found


def run(*args, env=None):
    print(">", Path(str(args[0])).name, *[str(a) for a in args[1:3]], "..." if len(args) > 3 else "")
    result = subprocess.run([str(a) for a in args], env=env)
    if result.returncode:
        sys.exit(f"échec de {Path(str(args[0])).name} (code {result.returncode})")


def dependencies() -> list:
    jars = []
    for name, url, sha256 in DEPENDENCIES:
        path = LIBS / name
        if not path.is_file():
            LIBS.mkdir(exist_ok=True)
            print(f"> téléchargement de {name}")
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read()
            if hashlib.sha256(data).hexdigest() != sha256:
                sys.exit(f"{name} : empreinte SHA-256 inattendue, fichier refusé.")
            path.write_bytes(data)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
            sys.exit(f"{path} : empreinte SHA-256 inattendue (fichier modifié ?) ; supprimez-le pour le retélécharger.")
        jars.append(path)
    return jars


def keystore_password() -> str:
    if KEY_PROPS.is_file():
        for line in KEY_PROPS.read_text(encoding="utf-8").splitlines():
            if line.startswith("password="):
                return line.split("=", 1)[1].strip()
    if KEYSTORE.exists():
        sys.exit(f"{KEYSTORE} existe mais {KEY_PROPS} (mot de passe) manque.")
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(24)
    run(
        jdk_tool("keytool"),
        "-genkeypair",
        "-keystore",
        KEYSTORE,
        "-storetype",
        "PKCS12",
        "-alias",
        ALIAS,
        "-keyalg",
        "RSA",
        "-keysize",
        "4096",
        "-validity",
        "36500",
        "-dname",
        "CN=Lezard IA, C=FR",
        "-storepass:env",
        "LZ_KS_PASS",
        "-keypass:env",
        "LZ_KS_PASS",
        env={**os.environ, "LZ_KS_PASS": password},
    )
    KEY_PROPS.write_text(
        f"# clé de signature de l'application Android : à sauvegarder avec le jks\npassword={password}\n",
        encoding="utf-8",
    )
    print(f"Nouvelle clé de signature : {KEYSTORE} (à sauvegarder hors du PC).")
    return password


def main():
    android = sdk()
    android_jar = android / "platforms" / PLATFORM / "android.jar"
    tools = max(
        (d for d in (android / "build-tools").iterdir() if (d / "lib" / "d8.jar").is_file()),
        key=lambda d: [int(x) for x in d.name.split(".") if x.isdigit()],
        default=None,
    )
    if tools is None:
        sys.exit("build-tools introuvable dans le SDK Android.")
    exe = ".exe" if os.name == "nt" else ""
    java, javac = jdk_tool("java"), jdk_tool("javac")
    jars = dependencies()

    shutil.rmtree(BUILD, ignore_errors=True)
    (BUILD / "gen").mkdir(parents=True)
    (BUILD / "classes").mkdir()
    (BUILD / "dex").mkdir()

    run(tools / f"aapt2{exe}", "compile", "--dir", HERE / "res", "-o", BUILD / "res.zip")
    run(
        tools / f"aapt2{exe}",
        "link",
        "-o",
        BUILD / "base.apk",
        "-I",
        android_jar,
        "--manifest",
        HERE / "AndroidManifest.xml",
        "--java",
        BUILD / "gen",
        "-A",
        HERE / "assets",
        "--auto-add-overlay",
        BUILD / "res.zip",
    )

    sources = sorted((HERE / "src").rglob("*.java")) + sorted((BUILD / "gen").rglob("*.java"))
    run(
        javac,
        "-encoding",
        "UTF-8",
        "--release",
        "11",
        "-nowarn",
        "-Xlint:-options",
        "-classpath",
        os.pathsep.join(str(p) for p in [android_jar, *jars]),
        "-d",
        BUILD / "classes",
        *sources,
    )
    # lecture des QR codes vérifiée sur le PC (QrDecoder n'utilise pas Android) avant d'emballer
    (BUILD / "check").mkdir()
    check_cp = os.pathsep.join(str(p) for p in [BUILD / "check", *jars])
    run(
        javac,
        "-encoding",
        "UTF-8",
        "--release",
        "11",
        "-nowarn",
        "-cp",
        check_cp,
        "-d",
        BUILD / "check",
        HERE / "src" / "fr" / "lezardia" / "app" / "QrDecoder.java",
        HERE / "tests" / "QrDecoderCheck.java",
    )
    run(java, "-cp", check_cp, "fr.lezardia.app.QrDecoderCheck")
    run(
        java,
        "-cp",
        tools / "lib" / "d8.jar",
        "com.android.tools.r8.D8",
        "--release",
        "--min-api",
        MIN_API,
        "--lib",
        android_jar,
        "--output",
        BUILD / "dex",
        *sorted((BUILD / "classes").rglob("*.class")),
        *jars,
    )

    unsigned = BUILD / "unsigned.apk"
    shutil.copy(BUILD / "base.apk", unsigned)
    with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as apk:
        apk.write(BUILD / "dex" / "classes.dex", "classes.dex")
    aligned = BUILD / "aligned.apk"
    run(tools / f"zipalign{exe}", "-p", "-f", "4", unsigned, aligned)

    out = BUILD / "lezard-ia.apk"
    env = {**os.environ, "LZ_KS_PASS": keystore_password()}
    run(
        java,
        "-jar",
        tools / "lib" / "apksigner.jar",
        "sign",
        "--ks",
        KEYSTORE,
        "--ks-key-alias",
        ALIAS,
        "--ks-pass",
        "env:LZ_KS_PASS",
        "--key-pass",
        "env:LZ_KS_PASS",
        "--out",
        out,
        aligned,
        env=env,
    )
    run(java, "-jar", tools / "lib" / "apksigner.jar", "verify", out)
    print(f"Application prête : {out} ({out.stat().st_size // 1024} Ko)")

    if "--install" in sys.argv[1:]:
        run(android / "platform-tools" / f"adb{exe}", "install", "-r", out)


if __name__ == "__main__":
    main()
