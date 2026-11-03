// Enregistrement d'un fichier produit par la page (exports, clé de chiffrement). Navigateur : lien <a download>.
// Application Android (android/) : la WebView ignore les liens blob: ; le fichier passe au pont LezardApp, qui
// demande à la personne où l'enregistrer.
export async function saveBlob(blob, name) {
  if (window.LezardApp?.saveFile) {
    const data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(blob);
    });
    window.LezardApp.saveFile(name, blob.type || "application/octet-stream", data);
    return;
  }
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

// bloc de code d'une réponse enregistré tel quel, avec l'extension de son langage (bouton « Télécharger »)
const CODE_EXT = { python: "py", py: "py", javascript: "js", js: "js", typescript: "ts", ts: "ts", tsx: "tsx", jsx: "jsx",
  html: "html", css: "css", json: "json", bash: "sh", sh: "sh", shell: "sh", powershell: "ps1", ps1: "ps1", sql: "sql",
  java: "java", c: "c", cpp: "cpp", "c++": "cpp", csharp: "cs", cs: "cs", go: "go", rust: "rs", ruby: "rb", php: "php",
  yaml: "yml", yml: "yml", xml: "xml", svg: "svg", markdown: "md", md: "md", mermaid: "mmd", toml: "toml", ini: "ini",
  csv: "csv", kotlin: "kt", swift: "swift", lua: "lua", r: "r", dockerfile: "Dockerfile" };

export function saveCode(button) {
  const text = document.getElementById(button.dataset.download)?.textContent ?? "";
  const ext = CODE_EXT[button.dataset.lang] || "txt";
  saveBlob(new Blob([text], { type: "text/plain;charset=utf-8" }), ext === "Dockerfile" ? ext : `code.${ext}`);
}
