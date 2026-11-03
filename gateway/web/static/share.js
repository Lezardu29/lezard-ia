// Conversation partagée par lien (lecture seule) : /partage/<jeton> lit /v1/shared/<jeton>, sans clé.
// Questions en texte brut ; réponses par le même rendu Markdown échappé que le chat ; réflexion des modèles retirée.
import { renderMarkdown } from "./markdown.js";
import { saveCode } from "./save-file.js";

const $ = (id) => document.getElementById(id);
const THINK = /^\s*<think>[\s\S]*?(<\/think>|$)/i;
const AVATAR = '<span class="lz-loader" aria-hidden="true"></span>';

const textOf = (content) => (typeof content === "string" ? content
  : (content || []).filter((p) => p.type === "text").map((p) => p.text).join("\n"));
const imagesOf = (content) => (typeof content === "string" ? []
  : (content || []).filter((p) => p.type === "image_url" && /^data:image\//.test(p.image_url?.url || ""))
    .map((p) => p.image_url.url));

function message(m) {
  const el = document.createElement("article");
  el.className = `msg ${m.role}`;
  if (m.role === "assistant") {
    el.innerHTML = `<div class="avatar">${AVATAR}</div><div class="content"><div class="body"></div></div>`;
    el.querySelector(".body").innerHTML = renderMarkdown(textOf(m.content).replace(THINK, "").trimStart());
    // aperçu (page HTML, diagramme) : propre au chat ; ici, le code reste affiché
    el.querySelectorAll(".code.previewable").forEach((c) => c.classList.remove("previewable"));
    el.querySelectorAll(".preview-btn, .code-kind").forEach((b) => b.remove());
    if (m.model) {
      const model = document.createElement("p");
      model.className = "share-model";
      model.textContent = m.model;                       // texte brut
      el.querySelector(".content").append(model);
    }
  } else {
    el.innerHTML = '<div class="user-col"><div class="body"></div></div>';
    const body = el.querySelector(".body");
    body.textContent = textOf(m.content);                // texte brut : jamais interprété
    for (const url of imagesOf(m.content)) {
      const img = document.createElement("img");
      img.className = "share-image";
      img.alt = "Image jointe";
      img.src = url;                                     // data:image/… seulement
      body.before(img);
    }
  }
  return el;
}

async function copy(button) {
  const code = document.getElementById(button.dataset.copy)?.textContent ?? "";
  try { await navigator.clipboard.writeText(code); } catch { return; }
  button.textContent = "Copié ✓";
  setTimeout(() => { button.textContent = "Copier"; }, 1500);
}

async function load() {
  const token = decodeURIComponent(location.pathname.split("/").pop() || "");
  let r = null, data = null;
  try {
    r = await fetch(`/v1/shared/${encodeURIComponent(token)}`, { referrerPolicy: "no-referrer" });
    data = await r.json();
  } catch { /* gateway injoignable */ }
  if (!r?.ok) {
    $("share-title").textContent = "Partage introuvable";
    $("share-date").textContent = data?.error?.message || "La gateway ne répond pas.";
    return;
  }
  document.title = `${data.title} · Lezard IA`;
  $("share-title").textContent = data.title;
  $("share-date").textContent = `Partagée le ${new Date(data.created_at * 1000).toLocaleDateString("fr-FR", { dateStyle: "long" })}`;
  $("messages").replaceChildren(...data.messages.map(message));
}

$("messages").addEventListener("click", (e) => {
  const b = e.target.closest(".copy");
  if (b) copy(b);
  const dl = e.target.closest(".code-dl");
  if (dl) saveCode(dl);
});
load();
