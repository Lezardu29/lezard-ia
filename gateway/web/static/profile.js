// Préférences personnelles et styles de réponse (comme ceux de Claude), enregistrés sur la gateway
// (/v1/preferences/profile, propres à la clé comme le thème). La page les envoie à l'IA avec chaque message, dans un
// message système (profileMessage) ; la gateway ne les ajoute jamais aux appels d'API.
// Fenêtre « Paramètres » : nom, ce que l'IA doit savoir, styles personnalisés. Choix du style : sélecteur de modèle.
import { api, errorMessage } from "./auth.js";

const $ = (id) => document.getElementById(id);
export const BUILTIN = [
  { id: "normal", name: "Normal", description: "Les réponses par défaut", instructions: "" },
  { id: "concis", name: "Concis", description: "Plus court, droit au but",
    instructions: "Réponds de façon brève et directe : l'essentiel d'abord, sans introduction ni conclusion, en "
      + "phrases courtes ; une liste seulement si elle aide." },
  { id: "explicatif", name: "Explicatif", description: "Pédagogique, étape par étape",
    instructions: "Réponds comme un enseignant patient : explique le raisonnement étape par étape, définis les termes "
      + "techniques, donne un exemple concret et termine par un court résumé." },
  { id: "formel", name: "Formel", description: "Soutenu et professionnel",
    instructions: "Réponds dans un registre soutenu et professionnel : vouvoiement, phrases complètes et structurées, "
      + "vocabulaire précis, sans familiarités ni émojis." },
];
const profile = { name: "", about: "", style: "normal", styles: [] };
const listeners = [];

export const onProfileChange = (fn) => listeners.push(fn);
export const displayName = () => profile.name;
export const styles = () => [...BUILTIN, ...profile.styles.map((s) => ({ ...s, description: "Votre style" }))];
export const activeStyle = () => styles().find((s) => s.id === profile.style) || BUILTIN[0];

export async function loadProfile() {
  const r = await api("/v1/preferences/profile");
  if (r.ok && r.data.profile) Object.assign(profile, { styles: [] }, r.data.profile);
  listeners.forEach((fn) => fn());
}

async function save(patch) {
  const next = { ...profile, ...patch };
  const r = await api("/v1/preferences/profile", { method: "PUT", body: next });
  if (r.ok) {
    Object.assign(profile, { styles: [] }, r.data.profile);
    listeners.forEach((fn) => fn());
  }
  return r;
}

export const setStyle = (id) => save({ style: id });

// message système ajouté en tête de chaque requête du chat (jamais enregistré dans la conversation)
export function profileMessage() {
  const parts = [];
  if (profile.name) parts.push(`La personne souhaite être appelée ${profile.name}.`);
  if (profile.about) parts.push(`Ce qu'elle souhaite que tu saches d'elle :\n${profile.about}`);
  const style = activeStyle();
  if (style.instructions) parts.push(`Style de réponse choisi (« ${style.name} ») : ${style.instructions}`);
  if (!parts.length) return null;
  return { role: "system", content: `Préférences de la personne : tiens-en compte sans les citer.\n\n${parts.join("\n\n")}` };
}

// ---------- fenêtre « Paramètres » ----------
let draft = [];                                         // styles personnalisés en cours de modification
let editing = null;                                     // style ouvert pour modification (un seul à la fois)

const smallButton = (text, onClick, cls = "btn btn-ghost btn-inline") => {
  const b = Object.assign(document.createElement("button"), { type: "button", className: cls, textContent: text });
  b.addEventListener("click", onClick);
  return b;
};

// liste compacte (nom, début des consignes, Modifier / Supprimer) ; le style modifié s'ouvre sur place
function renderStyles() {
  $("pf-error").hidden = true;                          // la liste a changé : l'ancien message ne vaut plus
  $("pf-styles").replaceChildren(...draft.map((s, i) => {
    const li = document.createElement("li");
    li.className = "pf-style";
    if (s !== editing) {
      const text = document.createElement("div");
      text.className = "pf-style-text";
      const name = Object.assign(document.createElement("strong"), { textContent: s.name || "Sans nom" });
      const preview = Object.assign(document.createElement("span"), { className: "muted",
        textContent: s.instructions || "Aucune consigne" });
      text.append(name, preview);
      li.append(text, smallButton("Modifier", () => { editing = s; renderStyles(); focusEditor(); }),
        smallButton("Supprimer", () => { draft.splice(i, 1); if (editing === s) editing = null; renderStyles(); }));
      return li;
    }
    li.classList.add("editing");
    const name = Object.assign(document.createElement("input"), { value: s.name, maxLength: 40, placeholder: "Nom" });
    name.setAttribute("aria-label", "Nom du style");
    name.addEventListener("input", () => { s.name = name.value; });
    const text = Object.assign(document.createElement("textarea"), { value: s.instructions, maxLength: 2000, rows: 4,
      placeholder: "Consignes : ton, longueur, structure…" });
    text.setAttribute("aria-label", "Consignes du style");
    text.addEventListener("input", () => { s.instructions = text.value; });
    const done = smallButton("OK", () => { editing = null; renderStyles(); });
    li.append(name, text, done);
    return li;
  }));
  $("pf-add-style").disabled = draft.length >= 10;
}

const focusEditor = () => $("pf-styles").querySelector(".editing input")?.focus();

/** Nom vide, en double ou celui d'un style fourni : message, sinon null. */
function nameProblem(styles) {
  const builtin = new Set(BUILTIN.map((s) => s.name.toLocaleLowerCase("fr")));
  const seen = new Set();
  for (const s of styles) {
    const key = s.name.trim().toLocaleLowerCase("fr");
    if (!key || !s.instructions.trim()) return `Le style « ${s.name.trim() || "sans nom"} » a besoin d'un nom et de consignes.`;
    if (builtin.has(key)) return `« ${s.name.trim()} » est déjà un style fourni : choisissez un autre nom.`;
    if (seen.has(key)) return `Deux styles s'appellent « ${s.name.trim()} » : renommez-en un.`;
    seen.add(key);
  }
  return null;
}

function openSettings() {
  $("pf-name").value = profile.name;
  $("pf-about").value = profile.about;
  draft = profile.styles.map((s) => ({ ...s }));
  editing = null;
  renderStyles();
  $("pf-error").hidden = true;
  $("profile-dialog").showModal();
}

async function submitSettings(e) {
  e.preventDefault();
  const kept = draft.filter((s) => s.name.trim() || s.instructions.trim());
  const problem = nameProblem(kept);
  if (problem) {
    $("pf-error").textContent = problem;
    $("pf-error").hidden = false;
    return;
  }
  const ids = new Set(kept.map((s) => s.id));
  const r = await save({
    name: $("pf-name").value.trim(), about: $("pf-about").value.trim(), styles: kept,
    style: BUILTIN.some((s) => s.id === profile.style) || ids.has(profile.style) ? profile.style : "normal",
  });
  if (!r.ok) {
    $("pf-error").textContent = errorMessage(r);
    $("pf-error").hidden = false;
    return;
  }
  $("profile-dialog").close();
}

export function initProfile(button) {
  button.addEventListener("click", openSettings);
  $("profile-form").addEventListener("submit", submitSettings);
  $("pf-cancel").addEventListener("click", () => $("profile-dialog").close());
  $("pf-add-style").addEventListener("click", () => {
    editing = { id: `style-${Date.now().toString(36)}`, name: "", instructions: "" };
    draft.push(editing);
    renderStyles();
    focusEditor();
  });
  loadProfile();
}
