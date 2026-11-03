// Panneau « Conversations » (19.1) : liste des conversations enregistrées sur la gateway (titres déchiffrés par
// le serveur, une clé ne voit que les siennes), recherche, renommage, suppression, choix « dans ce navigateur ».
// Recherche plein texte (19.3) : titres et messages ; l'extrait trouvé s'affiche, le message est mis en évidence.
// Menu « ⋯ » de chaque conversation : favori (en tête de liste), renommer, ranger dans un projet, partager, supprimer.
import { api, errorMessage } from "./auth.js";
import { conversationDeleted, conversationMoved, conversationRenamed, currentConversation, forgetLeftover,
         importLeftover, localLeftover, newConversation, openConversation } from "./chat.js";
import { initRowMenus } from "./row-menu.js";

const $ = (id) => document.getElementById(id);
const MODE_KEY = "gw.chat.mode";                         // "local" : conversations gardées dans ce navigateur
const DOCK_KEY = "gw.chat.panel";                        // liste ouverte ou fermée sur grand écran
// liste ancrée à gauche : dès 1100 px, dès 900 px dans l'interface Claude (barre latérale, colonne d'icônes repliée)
const claudeUi = () => window.GatewayTheme?.ui?.() === "claude";
const WIDE = { classic: window.matchMedia("(min-width: 1100px)"), claude: window.matchMedia("(min-width: 900px)") };
const wide = { get matches() { return WIDE[claudeUi() ? "claude" : "classic"].matches; } };
// usable : liste proposée (conversations enregistrables, ou barre latérale de l'interface Claude dans tous les cas)
const ui = { me: null, server: false, query: "", items: [], timer: 0, refreshTimer: 0,
             usable: () => !!ui.me?.conversations?.stored || claudeUi() };

const read = (k) => { try { return localStorage.getItem(k); } catch { return null; } };
const write = (k, v) => { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch { /* ignoré */ } };

// conversations sur la gateway : possible avec une clé, sauf si l'utilisateur a choisi ce navigateur
export function serverMode(me) {
  return !!me.conversations?.stored && read(MODE_KEY) !== "local";
}

function when(ts) {
  const d = new Date(ts * 1000), now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString("fr-FR", d.getFullYear() === now.getFullYear()
    ? { day: "numeric", month: "short" } : { day: "2-digit", month: "2-digit", year: "numeric" });
}

export function setOpen(on, remember = true) {
  const panel = $("conv-panel");
  panel.classList.toggle("open", on);
  $("conv-btn").setAttribute("aria-expanded", String(on));
  document.body.classList.toggle("conv-docked", on && wide.matches);
  document.body.classList.toggle("conv-rail", !on && wide.matches && claudeUi() && ui.usable());  // repliée : icônes
  $("conv-backdrop").hidden = !on || wide.matches;
  const label = on ? "Replier la barre latérale" : "Ouvrir la barre latérale";
  $("sb-toggle").title = label;
  $("sb-toggle").setAttribute("aria-label", label);
  if (wide.matches && ui.usable() && remember) write(DOCK_KEY, on ? "open" : "closed");
  if (on && !wide.matches && !claudeUi()) $("conv-search").focus();
}
// panneau d'aperçu ouvert : la liste se replie pour lui laisser la place, comme dans Claude (choix gardé inchangé)
export function foldForPreview() {
  if (isOpen() && wide.matches) setOpen(false, false);
}
export const isOpen = () => $("conv-panel").classList.contains("open");
export const isDocked = () => wide.matches;

// interface changée (panneau Apparence) ou largeur franchie : liste ancrée, repliée ou en tiroir, selon l'interface
export function relayout() {
  $("conv-btn").hidden = !ui.usable();
  setOpen(ui.usable() && wide.matches && read(DOCK_KEY) !== "closed");
}

/** Période d'une conversation dans la liste (comme Claude) : Aujourd'hui, Hier, 7 / 30 derniers jours, puis le mois. */
function period(ts) {
  const d = new Date(ts * 1000), today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.floor((today - new Date(d).setHours(0, 0, 0, 0)) / 86_400_000);
  if (days <= 0) return "Aujourd'hui";
  if (days === 1) return "Hier";
  if (days < 7) return "7 derniers jours";
  if (days < 30) return "30 derniers jours";
  const month = d.toLocaleDateString("fr-FR", d.getFullYear() === today.getFullYear()
    ? { month: "long" } : { month: "long", year: "numeric" });
  return month.charAt(0).toUpperCase() + month.slice(1);
}

function section(text) {
  const li = document.createElement("li");
  li.className = "conv-section";
  li.textContent = text;
  return li;
}

function render() {
  const list = $("conv-list");
  list.replaceChildren();
  $("conv-current").textContent = currentConversation().title || "";   // titre en haut (interface Claude)
  // favoris en tête (hors recherche), puis les autres, les plus récentes d'abord
  const starred = ui.query ? [] : ui.items.filter((c) => c.starred);
  const rest = ui.query ? ui.items : ui.items.filter((c) => !c.starred);
  if (starred.length) list.append(section("Favoris"), ...starred.map(item));
  let last = null;                                     // les autres, groupés par période (hors recherche)
  for (const c of rest) {
    const p = ui.query ? null : period(c.updated_at);
    if (p !== last) list.append(section(p));
    last = p;
    list.append(item(c));
  }
  const empty = $("conv-empty");
  empty.hidden = ui.items.length > 0;
  empty.textContent = ui.query ? "Aucune conversation ne correspond." : "Vos conversations apparaîtront ici, sur tous vos appareils.";
}

function item(c) {
  const current = currentConversation().id;
  const li = document.createElement("li");
  li.className = "conv-item";
  li.setAttribute("aria-current", String(c.id === current));
  const open = document.createElement("button");
  open.type = "button";
  open.className = "conv-open";
  const title = document.createElement("span");
  title.className = `conv-title${c.title ? "" : " untitled"}`;
  title.textContent = c.title || "Sans titre";               // texte brut : jamais interprété
  const date = document.createElement("span");
  date.className = "conv-date";
  date.textContent = `${when(c.updated_at)} · ${c.message_count} message${c.message_count > 1 ? "s" : ""}`;
  open.append(title, date);
  if (c.match?.snippet) {                                    // extrait du message trouvé (19.3)
    const snip = document.createElement("span");
    snip.className = "conv-snippet";
    snip.textContent = c.match.snippet;
    open.append(snip);
  }
  open.addEventListener("click", () => select(c.id, c.match?.message ?? null));
  li.append(open, actions(c));
  return li;
}

// menu « ⋯ » (row-menu.js : placement, fermeture, feuille en bas de l'écran sur téléphone)
function actions(c) {
  const menu = document.createElement("details");
  menu.className = "row-menu conv-menu";
  const sum = document.createElement("summary");
  sum.textContent = "⋯";
  sum.setAttribute("aria-label", `Actions pour « ${c.title || "Sans titre"} »`);
  const box = document.createElement("div");
  box.className = "menu";
  const add = (label, fn, cls = "") => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.textContent = label;
    b.addEventListener("click", () => { menu.open = false; fn(); });
    box.append(b);
  };
  add(c.starred ? "Retirer des favoris" : "Ajouter aux favoris", () => star(c));
  add("Renommer", () => rename(c));
  if (ui.me?.conversations?.stored) add(c.project_id ? "Changer de projet" : "Ranger dans un projet", () => move(c));
  add("Partager", () => share(c));
  add("Supprimer", () => remove(c), "danger");
  menu.append(sum, box);
  return menu;
}

async function star(c) {
  const r = await api(`/v1/conversations/${encodeURIComponent(c.id)}`, { method: "PATCH", body: { starred: !c.starred } });
  if (!r.ok) return alert(errorMessage(r));
  refreshConversations();
}

// ---------- ranger dans un projet ----------
async function move(c) {
  const r = await api("/v1/projects");
  if (!r.ok) return alert(errorMessage(r));
  const select = $("mv-project");
  select.replaceChildren(new Option("Aucun projet", ""), ...r.data.data.map((p) => new Option(p.name, p.id)));   // texte brut
  select.value = c.project_id || "";
  $("mv-error").hidden = true;
  ui.moving = c;
  $("move-dialog").showModal();
}

async function submitMove(e) {
  e.preventDefault();
  const c = ui.moving, project = $("mv-project").value || null;
  const r = await api(`/v1/conversations/${encodeURIComponent(c.id)}`, { method: "PATCH", body: { project } });
  if (!r.ok) {
    $("mv-error").textContent = errorMessage(r);
    $("mv-error").hidden = false;
    return;
  }
  $("move-dialog").close();
  conversationMoved(c.id, project);
  refreshConversations();
}

// ---------- partage par lien, en lecture seule ----------
async function share(c) {
  ui.sharing = c;
  $("sh-error").hidden = true;
  const r = await api(`/v1/conversations/${encodeURIComponent(c.id)}/share`);
  showShare(r.ok ? r.data : { shared: false });
  $("share-dialog").showModal();
}

function showShare(state) {
  const local = /^(localhost|127\.|\[::1\])/.test(location.hostname);
  $("sh-text").textContent = state.shared
    ? "Toute personne qui a ce lien peut lire cette conversation telle qu'elle était au moment du partage."
    : "Un lien permet à n'importe qui de lire cette conversation, sans clé ni compte. Vous pourrez le retirer à tout moment.";
  $("sh-link-row").hidden = !state.shared;
  $("sh-link").value = state.shared ? location.origin + state.path : "";
  $("sh-remove").hidden = !state.shared;
  $("sh-create").textContent = state.shared ? "Mettre à jour" : "Créer le lien";
  $("sh-note").textContent = !state.shared ? ""
    : (local ? "Adresse de ce PC : ouvrez Lezard IA par son adresse d'accès distant pour un lien utilisable ailleurs. " : "")
      + "« Mettre à jour » copie la conversation actuelle et remplace le lien.";
}

async function shareAction(method) {
  const r = await api(`/v1/conversations/${encodeURIComponent(ui.sharing.id)}/share`, { method });
  if (!r.ok) {
    $("sh-error").textContent = errorMessage(r);
    $("sh-error").hidden = false;
    return;
  }
  $("sh-error").hidden = true;
  showShare(method === "DELETE" ? { shared: false } : { shared: true, ...r.data });
}

async function copyLink() {
  try { await navigator.clipboard.writeText($("sh-link").value); } catch { $("sh-link").select(); return; }
  $("sh-copy").textContent = "Copié ✓";
  setTimeout(() => { $("sh-copy").textContent = "Copier"; }, 1500);
}

export async function refreshConversations() {
  if (!ui.server) return;
  const q = ui.query ? `&in=messages&q=${encodeURIComponent(ui.query)}` : "";
  const r = await api(`/v1/conversations?limit=200${q}`);
  if (!r.ok) return;
  ui.items = r.data.data;
  render();
}
// appelée par le chat à chaque enregistrement : un seul rafraîchissement par rafale
export function conversationsChanged() {
  clearTimeout(ui.refreshTimer);
  ui.refreshTimer = setTimeout(refreshConversations, 300);
}

async function select(id, focus = null) {
  if (await openConversation(id, focus)) {
    render();
    if (!wide.matches) setOpen(false);
  }
}

async function rename(c) {
  const title = prompt("Nouveau titre :", c.title || "");
  if (title === null || !title.trim()) return;
  const r = await api(`/v1/conversations/${encodeURIComponent(c.id)}`, { method: "PATCH", body: { title } });
  if (!r.ok) return alert(errorMessage(r));
  conversationRenamed(c.id, r.data.title);
  refreshConversations();
}

async function remove(c) {
  if (!confirm(`Supprimer « ${c.title || "Sans titre"} » ? Elle disparaît de tous vos appareils.`)) return;
  const r = await api(`/v1/conversations/${encodeURIComponent(c.id)}`, { method: "DELETE" });
  if (!r.ok && r.status !== 404) return alert(errorMessage(r));
  conversationDeleted(c.id);
  refreshConversations();
}

export function offerImport() {
  // conversation gardée dans ce navigateur avant les conversations enregistrées : à reprendre ou à jeter
  const leftover = localLeftover();
  const box = $("conv-import");
  box.hidden = !leftover.length;
  if (!leftover.length) return;
  $("conv-import-text").textContent = `Une conversation de ${leftover.length} message${leftover.length > 1 ? "s" : ""} `
    + "n'est gardée que dans ce navigateur.";
  $("conv-import-yes").onclick = async () => {
    const r = await importLeftover();
    if (!r.ok) return alert(errorMessage(r));
    box.hidden = true;
    refreshConversations();
  };
  $("conv-import-no").onclick = () => {
    if (!confirm("Supprimer définitivement la conversation gardée dans ce navigateur ?")) return;
    forgetLeftover();
    box.hidden = true;
  };
}

export function startNewConversation() {
  newConversation();
  render();
  if (!wide.matches) setOpen(false);
}

export function initConversations(me, server) {
  ui.me = me;
  ui.server = server;
  $("conv-btn").addEventListener("click", () => setOpen(!isOpen()));
  $("sb-toggle").addEventListener("click", () => setOpen(!isOpen()));
  $("conv-close").addEventListener("click", () => setOpen(false));
  $("conv-backdrop").addEventListener("click", () => setOpen(false));
  $("conv-new").addEventListener("click", startNewConversation);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && isOpen() && !wide.matches) setOpen(false); });
  for (const q of Object.values(WIDE)) q.addEventListener("change", relayout);
  relayout();
  if (!me.conversations?.stored) {                     // sans clé : dans le navigateur, comme avant
    $("conv-search").hidden = $("conv-foot").hidden = true;
    $("conv-empty").hidden = false;
    $("conv-empty").textContent = "Connectez-vous avec une clé pour retrouver vos conversations sur tous vos appareils.";
    return;
  }

  const toggle = $("conv-store");
  toggle.checked = server;
  toggle.addEventListener("change", () => {
    const msg = toggle.checked
      ? "Enregistrer vos conversations sur la gateway ? Elles seront chiffrées et retrouvées sur tous vos appareils."
      : "Garder les nouvelles conversations dans ce navigateur seulement ? Celles déjà enregistrées restent sur la gateway.";
    if (!confirm(msg)) { toggle.checked = !toggle.checked; return; }
    write(MODE_KEY, toggle.checked ? null : "local");
    location.reload();
  });
  const days = me.conversations.retention_days;
  $("conv-note").textContent = !server ? "Conversations gardées dans ce navigateur seulement."
    : `Chiffrées sur la gateway, propres à cette clé.${days ? ` Effacées après ${days} jours sans activité.` : ""}`;
  $("conv-search").hidden = !server;
  if (!server) {
    $("conv-empty").hidden = false;
    $("conv-empty").textContent = "Activez l'enregistrement pour retrouver vos conversations sur tous vos appareils.";
    return;
  }
  initRowMenus();
  $("move-form").addEventListener("submit", submitMove);
  $("mv-cancel").addEventListener("click", () => $("move-dialog").close());
  $("sh-create").addEventListener("click", () => shareAction("POST"));
  $("sh-remove").addEventListener("click", () => shareAction("DELETE"));
  $("sh-close").addEventListener("click", () => $("share-dialog").close());
  $("sh-copy").addEventListener("click", copyLink);
  $("conv-search").addEventListener("input", (e) => {
    clearTimeout(ui.timer);
    ui.timer = setTimeout(() => { ui.query = e.target.value.trim(); refreshConversations(); }, 250);
  });
  refreshConversations();
}
