// Cercle de contexte (barre du chat) : part de la fenêtre du modèle qu'occupe la conversation envoyée à l'IA (depuis
// le dernier /compacte). Un appui ouvre le panneau « Utilisation » (usage-panel.js : contexte, plafonds de la clé,
// bouton « Compacter » qui lance /compacte) ; à 90 %, le chat compacte de lui-même.
// Discret comme chez Claude : caché tant que la conversation occupe moins d'un quart de la fenêtre (le panneau reste
// ouvert par « Utilisation » dans le menu du compte), pourcentage écrit au centre à partir de 50 %.
import { chatModels, forcedModel } from "./settings-bar.js";
import { initUsagePanel, openUsagePanel } from "./usage-panel.js";

const R = 8, C = 2 * Math.PI * R;                          // rayon et circonférence de l'anneau (viewBox 20×20)
const FALLBACK = 32_768;                                   // fenêtre inconnue (choix automatique, aucune réponse encore)
const WARN = 0.7, FULL = 0.9;                              // orange, puis rouge : compactage automatique
const SHOW_RING = 0.25;                                    // cercle visible à partir de là
const SHOW_PCT = 0.5;                                      // pourcentage écrit au centre à partir de là
let button = null, onCompact = () => {}, last = { tokens: 0, model: null }, windows = null;
const asked = new Set();                                   // modèles déjà cherchés : une relecture chacun

const fmt = (n) => Math.round(n).toLocaleString("fr-FR");

/** Fenêtres de contexte des modèles (/v1/models), relues si un modèle inconnu apparaît. */
async function loadWindows() {
  const list = await chatModels().catch(() => []);
  windows = new Map(list.filter((m) => m.metadata?.context_window).map((m) => [m.id, m.metadata.context_window]));
  paint();
}

export function mountContextRing(toolbar, compact) {
  onCompact = compact;
  button = document.createElement("button");
  button.type = "button";
  button.id = "context-ring";
  button.className = "context-ring";
  button.innerHTML = `<svg viewBox="0 0 20 20" aria-hidden="true"><circle class="cr-track" cx="10" cy="10" r="${R}"/>`
    + `<circle class="cr-fill" cx="10" cy="10" r="${R}" stroke-dasharray="${C}" stroke-dashoffset="${C}"/></svg>`
    + '<span class="cr-pct" aria-hidden="true"></span>';
  initUsagePanel(() => onCompact(), () => ({ tokens: last.tokens, size: windowSize(), model: forcedModel() || last.model, auto: FULL }));
  button.addEventListener("click", () => openUsagePanel());
  document.getElementById("usage-btn")?.addEventListener("click", () => openUsagePanel());
  toolbar.append(button);
  document.addEventListener("gw-routing", paint);          // autre modèle imposé : autre fenêtre
  loadWindows();
}

/** tokens : estimation du contexte envoyé ; model : dernier modèle qui a répondu (« provider/modèle »). */
export function updateContextRing(tokens, model) {
  last = { tokens, model };
  if (windows && model && !windows.has(model) && !asked.has(model)) {   // modèle installé depuis : sa fenêtre
    asked.add(model);
    loadWindows();
  }
  paint();
}

/** Fenêtre du modèle qui recevra le prochain message ; null si inconnue. */
function windowSize() {
  return windows?.get(forcedModel() || last.model) || null;   // le modèle imposé, sinon le dernier qui a répondu
}

/** Part de la fenêtre occupée, seulement si la fenêtre est connue (le compactage automatique n'agit pas à l'aveugle). */
export function contextRatio() {
  const size = windowSize();
  return size ? last.tokens / size : 0;
}
export const AUTO_COMPACT = FULL;

function paint() {
  if (!button) return;
  const known = windowSize();
  const size = known || FALLBACK;
  const ratio = Math.min(1, last.tokens / size);
  button.querySelector(".cr-fill").setAttribute("stroke-dashoffset", String(C * (1 - ratio)));
  button.dataset.level = ratio >= FULL ? "full" : ratio >= WARN ? "warn" : "ok";
  button.hidden = ratio < SHOW_RING;
  const pct = Math.round(ratio * 100);
  button.classList.toggle("with-pct", ratio >= SHOW_PCT);
  button.querySelector(".cr-pct").textContent = ratio >= SHOW_PCT ? String(pct) : "";
  const of = known ? `${fmt(last.tokens)} / ${fmt(size)} tokens` : `environ ${fmt(last.tokens)} tokens, fenêtre du modèle inconnue`;
  const label = `Contexte : ${pct} % (${of}). Appuyer pour voir l'utilisation et compacter la conversation`
    + (known ? ` ; compactage automatique à ${Math.round(FULL * 100)} %` : "");
  button.title = label;
  button.setAttribute("aria-label", label);
}
