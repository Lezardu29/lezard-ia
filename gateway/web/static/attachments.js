// Pièces jointes du chat (19.2) : trombone, appareil photo (téléphone), glisser-déposer, coller une image.
// Images : réduites dans le navigateur puis envoyées dans le message (image_url) — la gateway choisit alors un modèle
// qui voit les images, local d'abord. Documents (PDF, DOCX, XLSX, HTML, Markdown, texte, code, CSV) : envoyés à /v1/documents dès l'ajout, puis
// cités par la requête (champ `documents`) : texte complet s'il tient, sinon les extraits les plus pertinents.
// Types et tailles revérifiés par le serveur.
import { errorMessage, getKey } from "./auth.js";

const MAX_ITEMS = 10;                         // par message
const MAX_IMAGE_INPUT = 25 * 1024 * 1024;     // photo brute avant réduction
const MAX_DOC = 20 * 1024 * 1024;             // DOCUMENT_MAX_BYTES par défaut (le serveur fait foi)
const MAX_SIDE = 1568;                        // côté maximal envoyé : au-delà, les modèles réduisent eux-mêmes
const KEEP_AS_IS = 1.5 * 1024 * 1024;         // image déjà petite : envoyée telle quelle
const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];

const items = [];
let opts = null;
let seq = 0;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

export const sizeLabel = (b) => (b >= 1e6 ? `${(b / 1e6).toFixed(1)} Mo` : `${Math.max(1, Math.round(b / 1e3))} ko`);

function isImage(file) {
  return file.type.startsWith("image/") || /\.(png|jpe?g|webp|gif|heic|heif|bmp)$/i.test(file.name);
}

function readDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(r.error);
    r.readAsDataURL(blob);
  });
}

async function prepareImage(file) {
  if (file.size > MAX_IMAGE_INPUT) throw new Error(`image trop lourde (${sizeLabel(file.size)}, max ${sizeLabel(MAX_IMAGE_INPUT)})`);
  let bitmap;
  try { bitmap = await createImageBitmap(file); } catch { throw new Error("format d'image non lu par ce navigateur"); }
  const { width, height } = bitmap;
  if (IMAGE_TYPES.includes(file.type) && file.size <= KEEP_AS_IS && Math.max(width, height) <= MAX_SIDE) {
    bitmap.close();
    return { url: await readDataUrl(file), width, height, size: file.size };
  }
  const scale = Math.min(1, MAX_SIDE / Math.max(width, height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  const g = canvas.getContext("2d");
  g.fillStyle = "#fff";                                 // transparence -> fond blanc (JPEG)
  g.fillRect(0, 0, canvas.width, canvas.height);
  g.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
  if (!blob) throw new Error("réduction de l'image impossible");
  return { url: await readDataUrl(blob), width: canvas.width, height: canvas.height, size: blob.size };
}

async function uploadDoc(file) {
  if (file.size > MAX_DOC) throw new Error(`fichier trop lourd (${sizeLabel(file.size)}, max ${sizeLabel(MAX_DOC)})`);
  const form = new FormData();
  form.append("file", file, file.name);
  const key = getKey();
  let res;
  try {
    res = await fetch("/v1/documents", { method: "POST", body: form,
                                         headers: key ? { Authorization: `Bearer ${key}` } : {} });
  } catch {
    throw new Error("gateway injoignable");
  }
  let data = null;
  try { data = await res.json(); } catch { /* corps vide */ }
  if (!res.ok) throw new Error(errorMessage({ status: res.status, data }));
  return { id: data.id, filename: data.filename, kind: data.kind, size_bytes: data.size_bytes };
}

function deleteDoc(id) {
  const key = getKey();
  fetch(`/v1/documents/${encodeURIComponent(id)}`, { method: "DELETE",
                                                     headers: key ? { Authorization: `Bearer ${key}` } : {} })
    .catch(() => {});                                   // nettoyage : sans conséquence s'il échoue
}

function render() {
  const tray = opts.tray;
  tray.replaceChildren();
  tray.hidden = !items.length;
  for (const it of items) {
    const chip = el("div", `attachment ${it.kind} ${it.status}`);
    chip.setAttribute("role", "listitem");
    if (it.kind === "image" && it.url) {
      const img = el("img");
      img.src = it.url;
      img.alt = "";
      chip.append(img);
    } else {
      chip.append(el("span", "attachment-icon", it.kind === "image" ? "IMG" : (it.ext || "DOC")));
    }
    const text = el("span", "attachment-text");
    text.append(el("span", "attachment-name", it.name));
    text.append(el("span", "attachment-sub", it.status === "loading" ? (it.kind === "image" ? "préparation…" : "envoi…")
      : it.status === "error" ? it.error : sizeLabel(it.size)));
    chip.title = it.status === "error" ? `${it.name} : ${it.error}` : it.name;
    const remove = el("button", "attachment-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Retirer ${it.name}`);
    remove.addEventListener("click", () => removeItem(it));
    chip.append(text, remove);
    tray.append(chip);
  }
  opts.onChange();
}

function removeItem(it) {
  const i = items.indexOf(it);
  if (i < 0) return;
  items.splice(i, 1);
  it.removed = true;
  if (it.doc) deleteDoc(it.doc.id);                     // envoyé mais pas utilisé : retiré de la gateway
  render();
  opts.field.focus();
}

export function addFiles(files) {
  for (const file of files) {
    if (items.length >= MAX_ITEMS) {
      opts.onNotice(`${MAX_ITEMS} pièces jointes au plus par message.`);
      break;
    }
    const kind = isImage(file) ? "image" : "doc";
    const it = { key: ++seq, kind, name: file.name || (kind === "image" ? "image collée" : "document"),
                 size: file.size, status: "loading", ext: (file.name.split(".").pop() || "").slice(0, 4).toUpperCase() };
    items.push(it);
    const job = kind === "image" ? prepareImage(file) : uploadDoc(file);
    job.then((r) => {
      if (kind === "image") Object.assign(it, { url: r.url, size: r.size });
      else if (it.removed) return deleteDoc(r.id);      // retiré pendant l'envoi
      else Object.assign(it, { doc: r, size: r.size_bytes });
      it.status = "ready";
    }).catch((e) => {
      it.status = "error";
      it.error = e.message || "échec";
    }).finally(() => { if (!it.removed) render(); });
  }
  render();
}

export function hasAttachments() {
  return items.some((it) => it.status === "ready");
}

// prêt à envoyer : rien en cours de préparation (une pièce en erreur est simplement ignorée)
export function attachmentsReady() {
  return !items.some((it) => it.status === "loading");
}

// pièces prêtes pour le message, et vide le plateau (les documents restent sur la gateway)
export function takeAttachments() {
  const ready = items.filter((it) => it.status === "ready");
  const out = { images: ready.filter((it) => it.kind === "image").map((it) => it.url),
                docs: ready.filter((it) => it.kind === "doc").map((it) => it.doc) };
  items.length = 0;
  render();
  return out;
}

// écran tactile : le « + » ouvre une feuille en bas de l'écran (fichier ou photo), comme l'application Claude
let sheet = null;
function attachSheet(fileInput, cameraInput) {
  if (sheet) return sheet;
  sheet = document.createElement("dialog");
  sheet.className = "attach-sheet";
  sheet.setAttribute("aria-label", "Ajouter au message");
  const item = (label, input) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.addEventListener("click", () => { sheet.close(); input.click(); });
    return b;
  };
  sheet.append(item("Prendre une photo", cameraInput), item("Joindre une image ou un document", fileInput));
  sheet.addEventListener("click", (e) => { if (e.target === sheet) sheet.close(); });   // appui sur le fond
  document.body.append(sheet);
  return sheet;
}

export function initAttachments(options) {
  opts = { onChange: () => {}, onNotice: () => {}, ...options };
  const { button, fileInput, cameraInput, dropZone, field } = opts;
  button.hidden = false;
  const touch = cameraInput && document.documentElement.dataset.input === "touch";   // téléphone, tablette
  button.addEventListener("click", () => (touch ? attachSheet(fileInput, cameraInput).showModal() : fileInput.click()));
  fileInput.addEventListener("change", () => { addFiles([...fileInput.files]); fileInput.value = ""; });
  cameraInput?.addEventListener("change", () => { addFiles([...cameraInput.files]); cameraInput.value = ""; });
  field.addEventListener("paste", (e) => {                // capture d'écran collée
    const files = [...(e.clipboardData?.files || [])];
    if (!files.length) return;
    e.preventDefault();
    addFiles(files);
  });
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  dropZone.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth++;
    document.body.classList.add("dragging-files");
  });
  dropZone.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  dropZone.addEventListener("dragleave", () => {
    if (--depth <= 0) { depth = 0; document.body.classList.remove("dragging-files"); }
  });
  dropZone.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    document.body.classList.remove("dragging-files");
    addFiles([...e.dataTransfer.files]);
    field.focus();
  });
}
