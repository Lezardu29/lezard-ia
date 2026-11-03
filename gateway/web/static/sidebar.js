// Barre latérale de l'interface Claude, sur la page du chat : nouvelle conversation, recherche, conversations récentes
// et, en bas, le menu du compte où sont rangés les boutons de la barre du haut (data-drawer) et le choix
// d'enregistrement des conversations. Interface Lezard IA : tout revient à sa place (mêmes nœuds : leurs écouteurs
// restent attachés), et le tiroir du téléphone (mobile.js) les reprend.
import { holdDrawer } from "./mobile.js";
import { isDocked, isOpen, relayout, setOpen, startNewConversation } from "./conversations-ui.js";
import { displayName, onProfileChange } from "./profile.js";

const $ = (id) => document.getElementById(id);
const T = window.GatewayTheme;
const claudeUi = () => T?.ui?.() === "claude";
const state = { items: [], marks: [], mounted: false };

function mount() {
  if (state.mounted) return;
  holdDrawer(true);                                    // le tiroir rend d'abord les boutons à la barre du haut
  state.marks = state.items.map((item) => {
    const m = document.createComment("sidebar");
    item.before(m);
    return m;
  });
  $("acct-menu").append(...state.items);
  state.mounted = true;
}

function unmount() {
  if (!state.mounted) return;
  showMenu(false);
  state.items.forEach((item, i) => { state.marks[i].after(item); state.marks[i].remove(); });
  state.mounted = false;
  holdDrawer(false);                                   // téléphone : le tiroir les reprend
}

function apply() {
  if (claudeUi()) mount(); else unmount();
  relayout();
}

// ---------- menu du compte ----------
function showMenu(on) {
  $("acct-menu").hidden = !on;
  $("acct-btn").setAttribute("aria-expanded", String(on));
  if (on) $("acct-menu").querySelector("a[href]:not([hidden]), button:not([hidden]):not([disabled])")?.focus();
}

// nom affiché : le prénom des Paramètres (profile.js), sinon le nom de la clé
function showName(me) {
  const name = displayName() || $("who").textContent.trim() || (me.type === "account" ? "Compte" : "Lezard IA");
  $("acct-name").textContent = name;                   // texte brut : jamais interprété
  $("acct-avatar").textContent = name.charAt(0).toUpperCase();
}

function initAccount(me) {
  showName(me);
  onProfileChange(() => showName(me));
  $("acct-plan").textContent = $("plan").textContent;
  $("acct-btn").addEventListener("click", () => showMenu($("acct-menu").hidden));
  // une action du menu (lien, panneau Apparence, déconnexion…) le referme, et le tiroir avec ; elle a déjà reçu le clic
  $("acct-menu").addEventListener("click", (e) => {
    if (!e.target.closest("a, button")) return;
    showMenu(false);
    if (!isDocked()) setOpen(false);
  });
  document.addEventListener("mousedown", (e) => {
    if (!$("acct-menu").hidden && !$("sb-account").contains(e.target)) showMenu(false);
  });
  $("sb-account").addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || $("acct-menu").hidden) return;
    e.stopPropagation();                               // ne ferme pas aussi le tiroir
    showMenu(false);
    $("acct-btn").focus();
  });
}

// ---------- recherche : le champ n'apparaît qu'à la demande, comme dans Claude ----------
function search() {
  if (!isOpen()) setOpen(true);                        // repliée ou en tiroir : la barre s'ouvre d'abord
  $("conv-panel").classList.add("searching");
  $("conv-search").focus();
}

function initSearch() {
  const field = $("conv-search");
  $("sb-search").addEventListener("click", search);
  field.addEventListener("blur", () => { if (!field.value) $("conv-panel").classList.remove("searching"); });
  field.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !field.value) return;
    e.stopPropagation();
    field.value = "";
    field.dispatchEvent(new Event("input"));
  });
}

// ---------- raccourcis clavier (ceux de Claude) ----------
function initShortcuts() {
  document.addEventListener("keydown", (e) => {
    if (!claudeUi() || !(e.ctrlKey || e.metaKey) || e.altKey) return;
    const key = e.key.toLowerCase();
    if (key === "k" && !e.shiftKey && !$("conv-search").hidden) { e.preventDefault(); search(); }
    else if (key === "o" && e.shiftKey) { e.preventDefault(); startNewConversation(); }
  });
}

export function initSidebar(me) {
  // sur téléphone, ils sont peut-être déjà dans le tiroir (mobile.js) : mount() les lui reprend
  state.items = [...document.querySelectorAll("[data-drawer]:not(#who)"), $("conv-foot")];
  initAccount(me);
  initSearch();
  initShortcuts();
  $("sb-new").addEventListener("click", startNewConversation);
  let last = claudeUi();
  T?.onChange?.(() => { if (claudeUi() !== last) { last = claudeUi(); apply(); } });   // panneau Apparence
  apply();
}
