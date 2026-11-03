// Sélecteur de modèle (interface Claude) : ouvert par la pastille « Auto · mode » ou celle du modèle imposé. Feuille en
// bas de l'écran sur téléphone, fenêtre au centre sinon. Choix automatique (mode), modèle imposé, compétences,
// conversation privée, effort et multi-agents : les mêmes réglages que /mode, /modele, /competence, /prive, /effort et /multi.
// Style de réponse : profile.js (Paramètres).
import { activeSkills, chatModels, forcedModel, routing, setEffort, setForcedModel, setMode, setMulti, setSkill,
         skillCatalog } from "./settings-bar.js";
import { isPrivate, notice, setPrivate } from "./chat.js";
import { activeStyle, setStyle, styles } from "./profile.js";

const MODES = {
  balanced: ["Équilibré", "Le meilleur rapport qualité-prix pour chaque question"],
  speed: ["Vitesse", "L'IA qui répond le plus vite"],
  economy: ["Éco", "La moins chère : vos IA locales d'abord"],
};
const EFFORTS = [["auto", "Auto"], ["low", "Bas"], ["medium", "Moyen"], ["high", "Élevé"]];
const MULTI = [["auto", "Auto"], ["multi", "Toujours"], ["single", "Jamais"]];
const CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 5 5L20 7"/></svg>';
const SEARCH_FROM = 8;   // au-delà, un champ de recherche
const FOLDED = 3;        // modèles montrés par groupe avant « Afficher plus »
const CHEVRON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';

let dialog = null;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;      // texte brut : jamais interprété
  return e;
}

/** Description courte d'un modèle : où il tourne, ce qu'il coûte, ce qu'il sait faire. */
export function describe(meta = {}) {
  const parts = [meta.hosting === "local" ? "Sur ce PC, gratuit" : meta.free ? "Cloud, gratuit dans la limite des quotas"
    : meta.input_per_mtok_usd || meta.output_per_mtok_usd
      ? `Cloud, ${price(meta.input_per_mtok_usd)} $ / ${price(meta.output_per_mtok_usd)} $ par million de tokens`
      : "Cloud"];
  if (meta.vision) parts.push("images");
  if (meta.reasoning) parts.push("raisonnement");
  return parts.join(" · ");
}
const price = (v) => (v || 0).toLocaleString("fr-FR", { maximumFractionDigits: 2 });

function option(title, desc, checked, onPick) {
  const b = el("button", "mp-option");
  b.type = "button";
  b.setAttribute("role", "menuitemradio");
  b.setAttribute("aria-checked", String(checked));
  const text = el("span", "mp-text");
  text.append(el("span", "mp-title", title), el("span", "mp-desc", desc));
  const tick = el("span", "mp-check");
  if (checked) tick.innerHTML = CHECK;
  b.append(text, tick);
  b.addEventListener("click", () => { onPick(); close(); });
  return b;
}

function group(label, items) {
  const box = el("div", "mp-group");
  box.setAttribute("role", "group");
  box.setAttribute("aria-label", label);
  box.append(...items);
  return [el("h3", "mp-label", label), box];
}

/** Groupe replié : les FOLDED premiers éléments (plus ceux que `keep` garde : le modèle imposé, les compétences
 *  actives), puis « Afficher plus » ; dépliable et repliable. */
function foldable(label, list, keep, item, extra = []) {
  const open = expanded.has(label) || list.length <= FOLDED + 1;   // une seule ligne cachée : autant la montrer
  const shown = open ? list : list.filter((m, i) => i < FOLDED || keep(m));
  const items = shown.map(item);
  if (list.length > FOLDED + 1) {
    const more = el("button", "mp-more");
    more.type = "button";
    more.setAttribute("aria-expanded", String(open));
    more.append(el("span", "", open ? "Afficher moins" : `Afficher plus (${list.length - shown.length})`));
    const chev = el("span", "mp-chev");
    chev.innerHTML = CHEVRON;
    more.append(chev);
    more.addEventListener("click", () => {
      if (open) expanded.delete(label); else expanded.add(label);
      fill();
      dialog.querySelector(`.mp-group[aria-label="${label}"] .mp-more`)?.focus();
    });
    items.push(more);
  }
  return [...group(label, items), ...extra];
}

/** Ligne à interrupteur (case à cocher role=switch) : onChange(coché) renvoie false pour refuser le changement. */
function switchRow(cls, title, desc, checked, disabled, onChange) {
  const row = el("label", `mp-row mp-toggle ${cls}`);
  const text = el("span", "mp-text");
  text.append(el("span", "mp-row-label", title), el("span", "mp-desc", desc));
  const sw = el("input", "mp-switch");
  sw.type = "checkbox";
  sw.setAttribute("role", "switch");
  sw.checked = checked;
  sw.disabled = disabled;
  sw.addEventListener("change", () => {
    if (onChange(sw.checked) === false) sw.checked = !sw.checked;
    fill();
  });
  row.append(text, sw);
  return row;
}

/** Interrupteur « Conversation privée » (/prive) : IA locales seulement. Clé limitée aux IA locales : toujours actif. */
function privateSwitch(hosting) {
  const forced = hosting === "local";
  return switchRow("mp-private", "Conversation privée",
    forced ? "Votre clé n'utilise que vos IA locales" : "Seules vos IA locales la reçoivent", forced || isPrivate(), forced,
    (on) => {
      setPrivate(on);
      notice(on ? "Mode privé activé : IA locales seulement." : "Mode privé désactivé.");
    });
}

/** Compétences (/competence) : consignes que l'IA suit, une ligne par compétence avec son origine open source. */
function skillsGroup() {
  const on = new Set(activeSkills());
  const item = (s) => switchRow("mp-skill", s.name, `${s.description} · d'après ${s.source.split(" (")[0]}`,
    on.has(s.id), false, (checked) => {
      if (setSkill(s.id, checked)) return notice(checked ? `Compétence activée : ${s.name}.` : `Compétence désactivée : ${s.name}.`);
      notice(`${skills.max_active} compétences actives au plus : désactivez-en une d'abord.`, true);
      return false;
    });
  const note = el("p", "mp-note mp-credits");
  const link = Object.assign(el("a", "", "Awesome Claude Skills"), { href: skills.catalog_url, target: "_blank", rel: "noopener" });
  note.append("Adaptées de projets open source (Superpowers, Caveman, Humanizer, UI UX Pro Max). D'autres idées : ", link, ".");
  return foldable("Compétences", skills.data, (s) => on.has(s.id), item, [note]);
}

function segmented(label, choices, value, allowed, onPick) {
  const row = el("div", "mp-row");
  const seg = el("div", "mp-seg");
  seg.setAttribute("role", "radiogroup");
  seg.setAttribute("aria-label", label);
  for (const [id, name] of choices) {
    const b = el("button", "", name);
    b.type = "button";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(id === value));
    b.disabled = !allowed(id);
    b.addEventListener("click", () => { onPick(id); fill(); });
    seg.append(b);
  }
  row.append(el("span", "mp-row-label", label), seg);
  return row;
}

let models = null, skills = null;
const expanded = new Set();   // groupes dépliés, gardés d'une ouverture à l'autre

function fill(filter = dialog.querySelector(".mp-search")?.value || "") {
  const body = dialog.querySelector(".mp-body");
  const r = routing(), forced = forcedModel(), q = filter.trim().toLowerCase();
  const nodes = [];
  if (!q) {
    nodes.push(...group("Automatique", r.modes.filter((m) => MODES[m]).map((m) =>
      option(`Auto · ${MODES[m][0]}`, MODES[m][1], !forced && r.mode === m, () => { setMode(m); setForcedModel(""); }))));
  }
  if (models === null) {
    nodes.push(el("p", "mp-note", "Chargement des modèles…"));
  } else {
    const shown = models.filter((m) => !q || m.id.toLowerCase().includes(q));
    const item = (m) => option(m.id.split("/").slice(1).join("/") || m.id, describe(m.metadata), forced === m.id,
                               () => setForcedModel(m.id));
    const local = shown.filter((m) => m.metadata?.hosting === "local"), cloud = shown.filter((m) => m.metadata?.hosting !== "local");
    const keep = (m) => m.id === forced;
    const fold = (label, list) => q ? group(label, list.map(item)) : foldable(label, list, keep, item);   // recherche : tout
    if (local.length) nodes.push(...fold("Sur ce PC", local));
    if (cloud.length) nodes.push(...fold("Cloud", cloud));
    if (!shown.length) nodes.push(el("p", "mp-note", q ? `Aucun modèle ne contient « ${filter.trim()} ».` : "Aucun modèle disponible."));
  }
  if (!q) {                                              // style de réponse (Paramètres : styles personnalisés)
    const current = activeStyle().id;
    nodes.push(...group("Style de réponse", styles().map((st) =>
      option(st.name, st.description, st.id === current, () => setStyle(st.id)))));
  }
  if (!q && skills?.data.length) nodes.push(...skillsGroup());
  const maxEffort = EFFORTS.findIndex(([id]) => id === r.maxEffort);
  const settings = el("div", "mp-group mp-settings");
  if (r.hosting !== "cloud") settings.append(privateSwitch(r.hosting));   // clé limitée au cloud : impossible
  settings.append(segmented("Effort", EFFORTS, r.effort,
    (id) => maxEffort < 0 || EFFORTS.findIndex(([e]) => e === id) <= maxEffort, setEffort));
  if (r.multiAllowed) settings.append(segmented("Multi-agents", MULTI, r.multi, () => true, setMulti));
  nodes.push(settings);
  body.replaceChildren(...nodes);
}

function build() {
  dialog = el("dialog", "model-picker");
  dialog.setAttribute("aria-labelledby", "mp-title");
  const head = el("div", "mp-head");
  const x = el("button", "icon-btn mp-close");
  x.type = "button";
  x.setAttribute("aria-label", "Fermer");
  x.innerHTML = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>';
  x.addEventListener("click", close);
  const title = el("h2", "", "Sélectionner un modèle");
  title.id = "mp-title";
  head.append(x, title, el("span"));
  const search = el("input", "mp-search");
  search.type = "search";
  search.placeholder = "Rechercher un modèle…";
  search.setAttribute("aria-label", "Rechercher un modèle");
  search.hidden = true;
  search.addEventListener("input", () => fill(search.value));
  const body = el("div", "mp-body");
  body.tabIndex = -1;                                   // focus à l'ouverture : la liste, pas le bouton ✕ (comme Claude)
  dialog.append(el("div", "mp-grip"), head, search, body);
  dialog.addEventListener("click", (e) => { if (e.target === dialog) close(); });   // clic sur le fond
  document.body.append(dialog);
}

function close() {
  if (dialog?.open) dialog.close();
}

export async function openModelPicker() {
  if (!dialog) build();
  const search = dialog.querySelector(".mp-search");
  search.value = "";
  fill();
  dialog.showModal();
  dialog.querySelector(".mp-body").focus();
  [models, skills] = await Promise.all([chatModels(), skillCatalog()]);   // modèles relus à chaque ouverture
  search.hidden = models.length < SEARCH_FROM;
  if (dialog.open) fill();
}
