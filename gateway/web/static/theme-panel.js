// Panneau de personnalisation du thème (5.3) + synchronisation avec le compte.
// Interface personnalisée : un .zip importé (ui_packs.py) habille les pages par-dessus le thème ; modèle et guide des
// connecteurs (static/guide-interface.md) à télécharger depuis le panneau.
import { api, errorMessage, getKey } from "./auth.js";

const T = window.GatewayTheme;
const MODES = [["light", "Clair"], ["dark", "Sombre"], ["auto", "Auto"]];
const UIS = [["classic", "Lezard IA"], ["claude", "Claude"]];
const COLORS = [["accent", "Accent"], ["background", "Arrière-plan"], ["bubble", "Bulles"], ["border", "Bordures"]];

let panel, button, statusEl, syncTimer = null, canSync = false, syncSeq = 0;

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  e.append(...children);
  return e;
}

// ---------- synchronisation serveur (dernier écrit gagne) ----------
function setStatus(text) { if (statusEl) statusEl.textContent = text; }

function scheduleSync(theme) {
  if (!canSync) { setStatus("Enregistré sur cet appareil"); return; }
  setStatus("Enregistrement…");
  clearTimeout(syncTimer);
  const mine = ++syncSeq;
  syncTimer = setTimeout(async () => {
    const { updated_at, ...body } = theme;
    const r = await api("/v1/preferences/theme", { method: "PUT", body });
    if (mine !== syncSeq) return;  // choix plus récent entre-temps : cette réponse ne doit pas l'écraser
    if (r.ok) {
      T.set(r.data.theme, { updatedAt: r.data.updated_at });
      setStatus("Enregistré sur votre compte");
    } else {
      setStatus("Enregistré sur cet appareil (synchronisation impossible)");
    }
  }, 600);
}

export async function syncTheme() {
  const r = await api("/v1/preferences/theme");
  if (!r.ok) return;
  canSync = true;
  const local = T.stored();
  const remote = r.data.theme ? { ...r.data.theme, updated_at: r.data.updated_at } : null;
  if (remote && (!local || (remote.updated_at || 0) > (local.updated_at || 0))) {
    T.set(remote, { updatedAt: remote.updated_at });           // un autre appareil a plus récent
  } else if (local && (!remote || (local.updated_at || 0) > (remote.updated_at || 0))) {
    scheduleSync(local);                                       // cet appareil a plus récent
  }
  refresh();
}

// ---------- modifications ----------
function update(patch) {
  const cur = T.get() || T.DEFAULT;
  const next = { mode: cur.mode, preset: cur.preset, custom: { ...(cur.custom || {}) }, ui: cur.ui, pack: cur.pack,
                 ...patch };
  scheduleSync(T.set(next));
  refresh();
}

function setCustom(field, value) {
  const color = value === null ? null : T.normalize(value);
  if (value !== null && !color) return false;
  const cur = T.get() || T.DEFAULT;
  const custom = { ...(cur.custom || {}) };
  // passer en « custom » conserve l'accent du préréglage en cours
  if (cur.preset !== "custom" && !custom.accent) custom.accent = T.palette(cur).vars["--accent"];
  if (color) custom[field] = color; else delete custom[field];
  update({ preset: "custom", custom });
  return true;
}

// ---------- rendu du panneau ----------
function build() {
  const uis = el("div", { class: "segmented segmented-2", role: "radiogroup", "aria-label": "Interface" },
    ...UIS.map(([value, label]) => el("button", {
      type: "button", role: "radio", "data-ui": value, onclick: () => update({ ui: value }),
    }, label)));
  const modes = el("div", { class: "segmented", role: "radiogroup", "aria-label": "Mode d'affichage" },
    ...MODES.map(([value, label]) => el("button", {
      type: "button", role: "radio", "data-mode": value, onclick: () => update({ mode: value }),
    }, label)));

  const presets = el("div", { class: "presets" },
    ...Object.entries(T.PRESETS).map(([id, p]) => {
      const sw = el("span", { class: "swatch", "aria-hidden": "true" });
      return el("button", { type: "button", class: "preset", "data-preset": id,
                            onclick: () => update({ preset: id, custom: {} }) }, sw, el("span", {}, p.label));
    }));

  const colors = el("div", { class: "colors" },
    ...COLORS.map(([field, label]) => {
      const id = `color-${field}`;
      const picker = el("input", { type: "color", id, "data-field": field, "aria-label": `${label} (sélecteur)` });
      const hex = el("input", { type: "text", class: "hex", "data-field": field, spellcheck: "false",
                                "aria-label": `${label} (HEX ou RGB)`, placeholder: "#rrggbb" });
      picker.addEventListener("input", () => setCustom(field, picker.value));
      hex.addEventListener("change", () => {
        if (!hex.value.trim()) return setCustom(field, null);
        hex.classList.toggle("invalid", !setCustom(field, hex.value));
      });
      return el("div", { class: "color-row" }, el("label", { for: id }, label), picker, hex);
    }));

  const packFile = el("input", { type: "file", accept: ".zip,application/zip", hidden: "", id: "pack-file" });
  packFile.addEventListener("change", () => { if (packFile.files[0]) importPack(packFile.files[0]); packFile.value = ""; });
  const packs = el("div", { class: "packs" },
    el("div", { class: "pack-list", id: "pack-list", role: "radiogroup", "aria-label": "Interface personnalisée" }),
    el("p", { class: "theme-note pack-status", id: "pack-status", role: "status" }),
    el("div", { class: "pack-actions" },
      el("button", { type: "button", class: "btn btn-ghost btn-inline", onclick: () => packFile.click() }, "Importer un .zip"),
      packFile,
      el("a", { class: "pack-link", href: "/ui-packs/modele.zip", download: "lezard-interface-modele.zip" }, "Modèle (.zip)"),
      el("a", { class: "pack-link", href: "/static/guide-interface.md", download: "guide-interface.md" },
        "Guide des connecteurs (.md)")));

  const reset = el("button", { type: "button", class: "btn btn-ghost btn-inline",
                               onclick: () => update({ preset: "default", custom: {} }) }, "Réinitialiser");
  statusEl = el("p", { class: "theme-status", role: "status" }, "");

  panel = el("div", { class: "theme-panel", id: "theme-panel", role: "dialog", "aria-modal": "false",
                      "aria-labelledby": "theme-title", hidden: "" },
    el("div", { class: "theme-head" },
      el("h2", { id: "theme-title" }, "Apparence"),
      el("button", { type: "button", class: "icon-btn", "aria-label": "Fermer", onclick: close }, "✕")),
    el("h3", {}, "Interface"), uis,
    el("p", { class: "theme-note", id: "ui-note", hidden: "" },
      "Interface Claude : la mise en page de Claude, avec ses couleurs par défaut. Choisissez un autre thème ou vos propres couleurs ci-dessous."),
    el("h3", {}, "Mode"), modes,
    el("h3", {}, "Thèmes"), presets,
    el("h3", {}, "Couleurs personnalisées"),
    el("p", { class: "theme-note", id: "pack-colors-note", hidden: "" },
      "L'interface personnalisée active peut remplacer ces couleurs."),
    colors,
    el("p", { class: "contrast", id: "contrast" }),
    el("h3", {}, "Interface personnalisée"),
    el("p", { class: "theme-note" },
      "Un .zip de styles, d'images et de polices qui habille Lezard IA (jamais de script). Partez du modèle ; le guide liste tout ce que la page utilise."),
    packs,
    el("div", { class: "theme-foot" }, statusEl, reset));
  document.body.appendChild(panel);
}

function refresh() {
  if (!panel) return;
  const t = T.get() || T.DEFAULT;
  const vars = T.palette(t).vars;
  panel.querySelectorAll("button[data-ui]").forEach((b) =>
    b.setAttribute("aria-checked", String(b.dataset.ui === T.ui())));
  panel.querySelector("#ui-note").hidden = T.ui() !== "claude";
  panel.querySelector("#pack-colors-note").hidden = !t.pack;
  renderPacks();
  panel.querySelectorAll("[data-mode]").forEach((b) =>
    b.setAttribute("aria-checked", String(b.dataset.mode === t.mode)));
  panel.querySelectorAll("[data-preset]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.preset === t.preset));
    const pal = T.palette({ mode: t.mode, preset: b.dataset.preset }).vars;     // vignettes au mode courant
    b.querySelector(".swatch").style.background = `linear-gradient(135deg, ${pal["--bg"]} 50%, ${pal["--accent"]} 50%)`;
  });
  const values = { accent: vars["--accent"], background: vars["--bg"], bubble: vars["--bubble-user"], border: vars["--border"] };
  panel.querySelectorAll("input[data-field]").forEach((i) => {
    const custom = (t.custom || {})[i.dataset.field];
    if (document.activeElement === i) return;
    if (i.type === "color") i.value = custom || values[i.dataset.field];
    else { i.value = custom || ""; i.placeholder = values[i.dataset.field]; i.classList.remove("invalid"); }
  });
  const r = T.report(t);
  const fmt = (label, v, min) => `${label} ${v}:1 ${v >= min ? "✓" : "⚠"}`;
  const c = panel.querySelector("#contrast");
  c.textContent = `Contraste — ${fmt("texte", r.text, 4.5)} · ${fmt("accent", r.accent, 3)} · ${fmt("bulles", r.bubble, 4.5)}`;
  c.classList.toggle("warn", r.text < 4.5 || r.accent < 3 || r.bubble < 4.5);
}

// ---------- interfaces personnalisées ----------
let packList = [];

function renderPacks() {
  const list = panel.querySelector("#pack-list");
  const active = (T.get() || {}).pack;
  const row = (id, name, desc, onDelete) => {
    const pick = el("button", { type: "button", role: "radio", class: "pack-pick", "aria-checked": String((active || "") === id),
                                onclick: () => choosePack(id) },
      el("span", { class: "pack-name" }, name), el("span", { class: "pack-desc" }, desc));   // texte brut
    const r = el("div", { class: "pack-row" }, pick);
    if (onDelete) r.append(el("button", { type: "button", class: "icon-btn pack-x", "aria-label": `Supprimer « ${name} »`,
                                         title: "Supprimer", onclick: onDelete }, "✕"));
    return r;
  };
  list.replaceChildren(
    row("", "Aucune", "Le thème choisi ci-dessus, sans habillage"),
    ...packList.map((p) => row(p.id, p.name, [p.version && `v${p.version}`, p.author, p.description].filter(Boolean).join(" · ")
                                || `${p.files} fichiers`, () => deletePack(p))));
}

async function loadPacks() {
  if (!getKey()) return;
  const r = await api("/v1/ui-packs");
  packList = r.ok ? r.data.data : [];
  if (panel) renderPacks();
}

function choosePack(id) {
  const p = packList.find((x) => x.id === id);
  update({ pack: id || undefined, ...(p?.interface ? { ui: p.interface } : {}) });   // interface prévue par le pack
  renderPacks();
}

async function importPack(file) {
  const status = panel.querySelector("#pack-status");
  status.textContent = `Import de « ${file.name} »…`;
  const form = new FormData();
  form.append("file", file, file.name);
  let res = null, data = null;
  try {
    const key = getKey();
    res = await fetch("/v1/ui-packs", { method: "POST", body: form, headers: key ? { Authorization: `Bearer ${key}` } : {} });
    data = await res.json();
  } catch { /* gateway injoignable ou réponse vide */ }
  if (!res?.ok) {
    status.textContent = errorMessage({ status: res?.status ?? 0, data });
    return;
  }
  status.textContent = `« ${data.name} » importée et appliquée.`;
  packList.push(data);
  choosePack(data.id);
}

async function deletePack(p) {
  if (!confirm(`Supprimer l'interface « ${p.name} » ?`)) return;
  const r = await api(`/v1/ui-packs/${encodeURIComponent(p.id)}`, { method: "DELETE" });
  if (!r.ok && r.status !== 404) { panel.querySelector("#pack-status").textContent = errorMessage(r); return; }
  packList = packList.filter((x) => x.id !== p.id);
  if ((T.get() || {}).pack === p.id) update({ pack: undefined });
  renderPacks();
}

function open() {
  refresh();
  loadPacks();
  panel.hidden = false;
  button.setAttribute("aria-expanded", "true");
  panel.querySelector("[aria-checked='true'], button")?.focus();
}
function close() {
  panel.hidden = true;
  button.setAttribute("aria-expanded", "false");
  button.focus();
}

export function initThemePanel(trigger) {
  button = trigger;
  build();
  button.setAttribute("aria-controls", "theme-panel");
  button.setAttribute("aria-expanded", "false");
  button.addEventListener("click", () => (panel.hidden ? open() : close()));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) close(); });
  document.addEventListener("mousedown", (e) => {
    if (!panel.hidden && !panel.contains(e.target) && !button.contains(e.target)) close();
  });
  T.onChange(refresh);
  setStatus("Enregistré sur cet appareil");
}
