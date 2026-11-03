// Panneau d'aperçu, à droite du chat (comme les « Artifacts » de Claude) : page HTML, image SVG ou diagramme Mermaid
// écrits par l'IA, affichés en vrai, avec leur code, les versions de la conversation, la copie et le téléchargement.
// Le contenu s'exécute dans /preview, isolé (core.PREVIEW_CSP + sandbox sans allow-same-origin) : il ne voit ni la
// clé ni l'API de la gateway. Il arrive par postMessage, jamais par l'adresse.
import { escapeHtml, highlight, PREVIEW_KINDS } from "./markdown.js";
import { saveBlob } from "./save-file.js";

const $ = (id) => document.getElementById(id);
const wide = window.matchMedia("(min-width: 900px)");     // à côté du chat ; en dessous : par-dessus, plein écran
const MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js";
const FILES = { html: ["page.html", "text/html"], svg: ["image.svg", "image/svg+xml"], mermaid: ["diagramme.mmd", "text/plain"] };
const state = { list: [], index: -1, item: null, frame: null, tab: "view", opener: null, onOpen: () => {} };

// blocs affichables de la conversation, dans l'ordre : les « versions » du panneau
function blocks() {
  return [...$("messages").querySelectorAll(".preview-btn")].map((b) => ({
    kind: b.dataset.kind, code: document.getElementById(b.dataset.preview)?.textContent ?? "",
  }));
}

function titleOf({ kind, code }) {
  const tag = kind === "html" && (code.match(/<title[^>]*>([^<]*)<\/title>/i) || code.match(/<h1[^>]*>([\s\S]*?)<\/h1>/i));
  const text = tag ? tag[1].replace(/<[^>]*>/g, "").trim() : "";
  return text.slice(0, 80) || PREVIEW_KINDS[kind];
}

// document complet donné à /preview
function documentOf({ kind, code }) {
  if (kind === "html") return code;
  const page = '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">' +
    "<style>html,body{margin:0;min-height:100%;background:#fff}body{display:grid;place-items:center;padding:16px;" +
    "box-sizing:border-box;min-height:100vh}svg{max-width:100%;height:auto}</style>";
  if (kind === "svg") return page + code;
  return `${page}<pre class="mermaid">${escapeHtml(code)}</pre><script src="${MERMAID}"></script>` +
    '<script>mermaid.initialize({ startOnLoad: true, securityLevel: "strict" });</script>';
}

function load() {
  state.frame?.remove();                               // un cadre neuf à chaque affichage : aucun reste du précédent
  const frame = document.createElement("iframe");
  frame.title = `Aperçu : ${titleOf(state.item)}`;
  frame.setAttribute("sandbox", "allow-scripts");
  frame.src = "/preview";
  state.frame = frame;
  $("preview-frame-wrap").append(frame);
}

window.addEventListener("message", (e) => {
  if (!state.frame || e.source !== state.frame.contentWindow || e.data?.type !== "gw-preview-ready") return;
  // origine opaque (sandbox) : « * » est la seule cible possible ; le contenu n'a rien de secret, c'est le code affiché
  state.frame.contentWindow.postMessage({ type: "gw-preview", html: documentOf(state.item) }, "*");
});

function showTab(tab) {
  state.tab = tab;
  $("preview-tab-view").setAttribute("aria-selected", String(tab === "view"));
  $("preview-tab-code").setAttribute("aria-selected", String(tab === "code"));
  $("preview-frame-wrap").hidden = tab !== "view";
  $("preview-code").hidden = tab !== "code";
}

function render() {
  const item = state.item;
  $("preview-title").textContent = titleOf(item);      // texte brut : jamais interprété
  $("preview-kind").textContent = PREVIEW_KINDS[item.kind];
  $("preview-versions").hidden = state.list.length < 2;
  $("preview-count").textContent = `${state.index + 1} / ${state.list.length}`;
  $("preview-prev").disabled = state.index <= 0;
  $("preview-next").disabled = state.index >= state.list.length - 1;
  $("preview-code").firstElementChild.innerHTML = highlight(item.code, item.kind === "mermaid" ? "" : item.kind);
  load();
}

function show(index) {
  state.list = blocks();
  if (!state.list[index]) return;
  state.index = index;
  state.item = state.list[index];
  showTab("view");                                     // chaque ouverture commence par l'aperçu, comme Claude
  if ($("preview-panel").hidden) state.onOpen();
  $("preview-panel").hidden = false;
  document.body.classList.toggle("preview-open", true);
  render();
}

export function closePreview() {
  if ($("preview-panel").hidden) return;
  $("preview-panel").hidden = true;
  document.body.classList.remove("preview-open");
  state.frame?.remove();
  state.frame = state.item = null;
  state.opener?.focus?.();
  state.opener = null;
}

function openFrom(button) {
  state.opener = button;
  show([...$("messages").querySelectorAll(".preview-btn")].indexOf(button));
  if (!wide.matches) $("preview-close").focus();
}

// réponse terminée : sa dernière page, image ou diagramme s'ouvre d'elle-même à côté du chat (grand écran)
export function previewAnswer(el) {
  const buttons = el?.querySelectorAll(".preview-btn");
  if (!buttons?.length || !wide.matches) return;
  show([...$("messages").querySelectorAll(".preview-btn")].indexOf(buttons[buttons.length - 1]));
}

async function copy() {
  try { await navigator.clipboard.writeText(state.item.code); } catch { return; }
  const b = $("preview-copy");
  b.classList.add("done");
  setTimeout(() => b.classList.remove("done"), 1500);
}

export function initPreview({ onOpen } = {}) {
  if (onOpen) state.onOpen = onOpen;
  $("messages").addEventListener("click", (e) => {
    if (e.target.closest(".copy")) return;              // « Copier » du bloc : géré par le chat
    const button = e.target.closest(".preview-btn")
      || e.target.closest(".code.previewable")?.querySelector(".preview-btn");   // carte de l'interface Claude
    if (button) openFrom(button);
  });
  $("preview-close").addEventListener("click", closePreview);
  $("preview-prev").addEventListener("click", () => show(state.index - 1));
  $("preview-next").addEventListener("click", () => show(state.index + 1));
  $("preview-tab-view").addEventListener("click", () => showTab("view"));
  $("preview-tab-code").addEventListener("click", () => showTab("code"));
  $("preview-copy").addEventListener("click", copy);
  $("preview-download").addEventListener("click", () => {
    const [name, type] = FILES[state.item.kind];
    saveBlob(new Blob([state.item.code], { type: `${type};charset=utf-8` }), name);
  });
  $("preview-panel").addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); closePreview(); } });
  // autre conversation, nouvelle conversation, fil réaffiché : le panneau se ferme dès qu'un message est retiré
  new MutationObserver((records) => {
    if (records.some((r) => [...r.removedNodes].some((n) => n.classList?.contains("msg")))) closePreview();
  }).observe($("messages"), { childList: true });
  showTab("view");
}
