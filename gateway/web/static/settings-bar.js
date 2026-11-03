// Réglages du routage (5.4) : mode, effort et multi-agents se règlent par /mode, /effort et /multi ; l'IA imposée
// par /modele ; les compétences actives par /competence ou le sélecteur de modèle. Respecte les droits de la clé.
import { api } from "./auth.js";

const STORE = "gw.chat.settings";
const MODES = [["speed", "Vitesse"], ["economy", "Éco"], ["balanced", "Équilibré"]];
const EFFORTS = ["auto", "low", "medium", "high"];
const DEFAULTS = { mode: "balanced", effort: "auto", orchestration: "auto", model: "", skills: [] };

let settings = structuredClone(DEFAULTS), updatedAt = 0;
let rights = {}, canSync = false, syncTimer = null;
const $ = (id) => document.getElementById(id);

// ---------- persistance (navigateur + compte, dernier écrit gagne) ----------
function loadLocal() {
  try {
    const s = JSON.parse(localStorage.getItem(STORE));
    if (s && typeof s === "object") { updatedAt = s.updated_at || 0; delete s.updated_at; return s; }
  } catch { /* ignoré */ }
  return null;
}
function persist() {
  updatedAt = Date.now() / 1000;
  try { localStorage.setItem(STORE, JSON.stringify({ ...settings, updated_at: updatedAt })); } catch { /* ignoré */ }
  if (!canSync) return;
  clearTimeout(syncTimer);
  syncTimer = setTimeout(async () => {
    const r = await api("/v1/preferences/chat", { method: "PUT", body: settings });
    if (r.ok) {
      updatedAt = r.data.updated_at;
      try { localStorage.setItem(STORE, JSON.stringify({ ...settings, updated_at: updatedAt })); } catch { /* ignoré */ }
    }
  }, 600);
}
async function sync() {
  const r = await api("/v1/preferences/chat");
  if (!r.ok) return;
  canSync = true;
  if (r.data.chat && (r.data.updated_at || 0) > updatedAt) {
    settings = mergeDefaults(r.data.chat);
    updatedAt = r.data.updated_at;
    try { localStorage.setItem(STORE, JSON.stringify({ ...settings, updated_at: updatedAt })); } catch { /* ignoré */ }
    enforceRights();
    render();
  } else if (updatedAt && (!r.data.chat || updatedAt > (r.data.updated_at || 0))) {
    persist();
  }
}
const mergeDefaults = (s) => ({ ...structuredClone(DEFAULTS), ...s });

// ---------- droits de la clé ----------
const allowedModes = () => rights.modes || MODES.map(([m]) => m);
const maxEffortIndex = () => (rights.max_effort ? EFFORTS.indexOf(rights.max_effort) : 3);
const multiAllowed = () => rights.orchestration !== false;  // inclus pour tous, sauf clé qui l'interdit

function enforceRights() {
  if (!allowedModes().includes(settings.mode)) settings.mode = allowedModes()[0];
  if (EFFORTS.indexOf(settings.effort) > maxEffortIndex()) settings.effort = EFFORTS[maxEffortIndex()];
  if (!multiAllowed()) settings.orchestration = "auto";
}

// ---------- barre du chat : le modèle imposé (/modele) ; « Auto · mode » sinon (interface Claude seulement) ----------
function build(container) {
  container.innerHTML = `
    <button type="button" class="chip auto-model" id="auto-model" title="Choisir l'IA qui répond (/modele)">
      <span class="auto-name"></span>
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
    </button>
    <button type="button" class="chip forced-model" id="forced-model" hidden title="Revenir au choix automatique">
      <span class="forced-name"></span><span class="forced-x" aria-hidden="true">✕</span>
    </button>`;
  // interface Claude : les deux pastilles ouvrent le sélecteur de modèle (comme Claude) ; sinon ✕ revient à l'automatique
  const picker = () => import("./model-picker.js").then((m) => m.openModelPicker());
  $("forced-model").addEventListener("click", () =>
    document.documentElement.dataset.ui === "claude" ? picker() : setForcedModel(""));
  $("auto-model").addEventListener("click", picker);
}

function render() {
  const forced = settings.model;
  const chip = $("forced-model");
  chip.hidden = !forced;
  $("auto-model").hidden = !!forced;
  const mode = (MODES.find(([id]) => id === settings.mode) || MODES[2])[1];
  $("auto-model").querySelector(".auto-name").textContent = `Auto · ${mode}`;
  chip.querySelector(".forced-name").textContent = forced.split("/").slice(1).join("/") || forced;
  chip.setAttribute("aria-label", document.documentElement.dataset.ui === "claude"
    ? `Modèle imposé : ${forced} — changer de modèle` : `Modèle imposé : ${forced} — revenir au choix automatique`);
  document.dispatchEvent(new Event("gw-routing"));       // cercle de contexte : fenêtre du modèle imposé
}

// ---------- API du module ----------
export function initSettingsBar(container, me) {
  rights = me.routing_rights || {};
  settings = mergeDefaults(loadLocal() || {});
  build(container);
  enforceRights();
  render();
  sync();
  // réglages changés par une autre page (Marketplace : compétences) : relus ici, sans recharger le chat
  window.addEventListener("storage", (e) => {
    if (e.key !== STORE) return;
    const s = loadLocal();
    if (!s) return;
    settings = mergeDefaults(s);
    enforceRights();
    render();
  });
}

/** Options gateway à joindre à chaque requête de chat. */
export function requestOptions() {
  const extra = settings.skills.length ? { skills: settings.skills } : {};
  if (settings.model) return { model: settings.model, effort: settings.effort, ...extra };   // modèle imposé : pas de routage
  const opts = { model: `auto/${settings.mode}`, effort: settings.effort, ...extra };
  if (multiAllowed()) opts.orchestration = settings.orchestration;
  return opts;
}

// ---------- compétences (consignes suivies par l'IA, GET /v1/skills) ----------
let catalog = null;
/** Compétences disponibles : { data: [{ id, name, description, group, source, url, license, tokens }], max_active }. */
export async function skillCatalog() {
  if (!catalog) {
    const r = await api("/v1/skills");
    if (!r.ok) return { data: [], max_active: 0 };
    catalog = r.data;
    const known = new Set(catalog.data.map((s) => s.id));     // compétence retirée de la gateway : oubliée
    if (settings.skills.some((id) => !known.has(id))) { settings.skills = settings.skills.filter((id) => known.has(id)); persist(); }
  }
  return catalog;
}
export function activeSkills() {
  return [...settings.skills];
}
/** Active ou coupe une compétence ; renvoie false si le plafond de compétences actives est atteint. */
export function setSkill(id, on) {
  const max = catalog?.max_active || 4;
  if (on && !settings.skills.includes(id)) {
    if (settings.skills.length >= max) return false;
    settings.skills = [...settings.skills, id];
  } else if (!on) {
    settings.skills = settings.skills.filter((s) => s !== id);
  }
  persist();
  return true;
}

/** Modèles de chat (embeddings et voix exclus), pour la commande /modele : relus à chaque fois (un modèle installé
 *  pendant la session apparaît tout de suite). */
export async function chatModels() {
  const r = await api("/v1/models");
  return (r.ok ? r.data.data : []).filter((m) => m.metadata?.chat !== false && !NOT_CHAT.test(m.id));
}

/** Variantes qui ne conversent pas (voix, image, transcription, audio en direct, recherche approfondie…) : retirées
 *  de la liste de /modele pour que les vrais modèles de chat restent faciles à trouver. */
const NOT_CHAT = /tts|image|live|audio|transcri|lyria|embed|robotics|deep-research|computer-use|nano-banana|whisper|moderation|antigravity/i;

/** Modèle imposé par /modele ; "" = choix automatique. */
export function forcedModel() {
  return settings.model;
}

export function setForcedModel(id) {
  settings.model = id;
  persist();
  render();
}

/** Réglages de routage pour les commandes /mode, /effort, /multi. */
export function routing() {
  return { mode: settings.mode, effort: settings.effort, multi: settings.orchestration,
           modes: allowedModes(), maxEffort: rights.max_effort || null, multiAllowed: multiAllowed(),
           hosting: rights.hosting || null };   // « local » / « cloud » : clé limitée (mode privé imposé / impossible)
}

/** Renvoie false si le mode n'est pas permis par la clé. */
export function setMode(id) {
  if (!allowedModes().includes(id)) return false;
  settings.mode = id;
  persist();
  render();
  return true;
}

/** Effort demandé, plafonné par la clé : renvoie l'effort effectivement choisi. */
export function setEffort(id) {
  settings.effort = EFFORTS[Math.min(EFFORTS.indexOf(id), maxEffortIndex())];
  persist();
  return settings.effort;
}

/** Multi-agents : "auto" (selon la complexité), "multi" (toujours), "single" (IA seule). false : non permis pour cette clé. */
export function setMulti(value) {
  if (!multiAllowed() || !["auto", "multi", "single"].includes(value)) return false;
  settings.orchestration = value;
  persist();
  return true;
}
