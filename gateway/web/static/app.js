import { api, clearKey, getKey } from "./auth.js";
import { attachmentsChanged, exportConversation, initChat, isPrivate, setPrivate } from "./chat.js";
import { initAttachments } from "./attachments.js";
import { conversationsChanged, foldForPreview, initConversations, isDocked, offerImport, serverMode, setOpen }
  from "./conversations-ui.js";
import { initProjects } from "./projects-ui.js";
import { displayName, initProfile, onProfileChange, profileMessage } from "./profile.js";
import { initThemePanel, syncTheme } from "./theme-panel.js";
import { initRemotePanel } from "./remote-panel.js";
import { initPhonePanel } from "./phone-panel.js";
import { initSettingsBar, requestOptions } from "./settings-bar.js";
import { initMobile } from "./mobile.js";
import { initSidebar } from "./sidebar.js";
import { initShortcutsHelp } from "./shortcuts.js";
import { initPreview, previewAnswer } from "./preview-panel.js";
import { initDictation } from "./dictation.js";
import { initPwa } from "./pwa.js";
import { offerUpdate, watchUpdates } from "./update-offer.js";

const $ = (id) => document.getElementById(id);

function logout() {
  clearKey();
  location.replace("/");
}

async function init() {
  if (!getKey()) return logout();
  const r = await api("/v1/me");
  if (!r.ok) return logout();          // clé révoquée, expirée, suspendue… : retour à la connexion
  const me = r.data;
  const admin = me.is_admin && !me.admin_session?.blocked;  // depuis Internet sans double authentification : non

  $("plan").textContent = me.edition === "gratuit" ? "Open source" : "Accès web";  // offre (29.4)
  $("who").textContent = me.key?.name || (me.type === "account" ? "Compte" : "");
  $("models-link").hidden = !admin;                    // page Modèles : clé maître
  if (admin) { offerUpdate(me); watchUpdates(); }      // nouvelle version : au lancement, puis toutes les 5 min


  initThemePanel($("theme-btn"));
  onProfileChange(greet);               // « Bonsoir, <nom> »
  initProfile($("settings-btn"));       // Paramètres : nom, préférences, styles de réponse
  if (admin) initRemotePanel($("remote-btn"));            // accès distant : clé maître uniquement
  initPhonePanel($("phone-btn"));                         // QR code pour l'application Android
  fillBugLink($("bug-link"), me);
  syncTheme();                          // récupère/pousse le thème du compte (multi-appareils)

  initSettingsBar($("toolbar"), me);
  const privateView = privateModeView(me.routing_rights?.hosting);
  showPlaceholder();

  // conversations enregistrées sur la gateway (19.1), sinon dans ce navigateur
  const server = serverMode(me);
  initConversations(me, server);
  initSidebar(me);                      // interface Claude : barre latérale et menu du compte
  initShortcutsHelp();                  // Ctrl+/ : raccourcis clavier
  initProjects(me, server, { onLeave: () => { if (!isDocked()) setOpen(false); } });   // projets (tiroir refermé)
  initChat({
    server,
    onChange: conversationsChanged,
    onNotice: (text) => { $("chat-notice").textContent = text; $("chat-notice").hidden = !text; },
    onPrivate: (on) => privateView?.render(on),
    onAuthError: logout,
    getOptions: requestOptions,         // mode / effort / multi-agents réglés par /mode, /effort, /multi
    getSystem: profileMessage,          // préférences personnelles et style de réponse (Paramètres)
    onMeta: (meta, el) => {
      if (meta) previewAnswer(el);                   // page, image ou diagramme : ouvert dans le panneau d'aperçu
    },
  }).then(() => { if (server) offerImport(); });   // conversation gardée dans ce navigateur : à importer ?
  // pièces jointes (19.2) : images (modèle vision) et documents (/v1/documents)
  initAttachments({ button: $("attach"), fileInput: $("attach-input"),
                    cameraInput: $("camera-input"), tray: $("attachments"), dropZone: document.body, field: $("prompt"),
                    onChange: attachmentsChanged,
                    onNotice: (text) => { $("chat-notice").textContent = text; $("chat-notice").hidden = !text; } });
  initExport();
  initPreview({ onOpen: foldForPreview });          // la liste se replie pour laisser la place
  // dictée vocale (13.9) : le texte transcrit est inséré dans le champ, jamais envoyé
  initDictation({ button: $("mic"), statusEl: $("dictation"), field: $("prompt"),
                  isPrivate: () => isPrivate() || me.routing_rights?.hosting === "local" });
}

// ---------- « Signaler un bug » : formulaire GitHub prérempli, que la personne relit et envoie elle-même ----------
// seulement la version (clé maître : jamais montrée aux autres comptes), le système et l'interface ; ni clé ni message
function fillBugLink(link, me) {
  const ua = navigator.userAgentData;
  const browser = ua?.brands?.filter((b) => !/Not.?A.?Brand/i.test(b.brand)).map((b) => `${b.brand} ${b.version}`)[0];
  const ui = window.GatewayTheme?.ui?.() === "claude" ? "interface Claude" : "interface Lezard IA";
  const system = [ua?.platform || navigator.platform, browser || navigator.userAgent.split(") ").pop(), ui,
                  `${window.innerWidth} px`].filter(Boolean).join(" · ");
  const url = new URL(link.href);
  if (me.version) url.searchParams.set("version", me.version);
  url.searchParams.set("systeme", system);
  link.href = url;
}

// ---------- export de la conversation affichée (19.3) ----------
function initExport() {
  const btn = $("export-btn"), menu = $("export-menu");
  const show = (on) => { menu.hidden = !on; btn.setAttribute("aria-expanded", String(on)); };
  btn.addEventListener("click", () => show(menu.hidden));
  menu.addEventListener("click", (e) => {
    const item = e.target.closest("[data-export]");
    if (!item) return;
    show(false);
    exportConversation(item.dataset.export);
  });
  document.addEventListener("click", (e) => { if (!$("export-wrap").contains(e.target)) show(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !menu.hidden) { show(false); btn.focus(); } });
}

// ---------- mode privé de la conversation : seules les IA locales (ce PC, réseau local) la reçoivent ----------
// activé et désactivé par /prive ; l'arrière-plan et la saisie le montrent
// texte d'aide de la saisie : le plus long qui tient dans le champ (le navigateur le couperait net, sans « … »),
// mesuré sur la largeur du champ et non de l'écran : barre latérale ouverte, zoom ou grande police comprises
const HINTS = {
  normal: ["Posez votre question… (/ pour les commandes)", "Posez votre question…", "Votre question…"],
  private: ["Conversation privée : seules vos IA locales la reçoivent…", "Conversation privée (IA locales)…",
            "Conversation privée…"],
};
const measure = document.createElement("canvas").getContext("2d");
let privateShown = false;
function showPlaceholder() {
  const field = $("prompt"), hints = HINTS[privateShown ? "private" : "normal"];
  const style = getComputedStyle(field);
  const room = field.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
  measure.font = style.font || `${style.fontSize} ${style.fontFamily}`;
  // champ caché (largeur nulle) : texte complet, recalculé dès qu'il s'affiche
  const fits = hints.find((h) => room <= 0 || measure.measureText(h).width <= room);
  field.placeholder = fits ?? hints.at(-1);
}
new ResizeObserver(() => showPlaceholder()).observe($("prompt"));
document.fonts?.ready.then(showPlaceholder);           // police chargée après coup : largeurs mesurées à nouveau

function privateModeView(hosting) {
  if (hosting === "cloud") return null;                 // clé limitée au cloud : mode privé impossible
  const forced = hosting === "local";                   // clé déjà limitée aux IA locales
  const render = (on) => {
    on = on || forced;
    document.body.classList.toggle("private-mode", on);
    privateShown = on;
    showPlaceholder();
  };
  return { render };
}

// salutation de l'accueil selon l'heure et le nom choisi dans les Paramètres, comme Claude
function greet() {
  const h = new Date().getHours(), name = displayName();
  document.querySelector(".welcome .gradient").textContent = (h >= 18 || h < 5 ? "Bonsoir" : "Bonjour")
    + (name ? `, ${name}` : "");          // textContent : le nom n'est jamais interprété
}

document.addEventListener("DOMContentLoaded", () => {
  greet();
  initMobile({ chatScroller: $("scroller") });         // tiroir + saisie au-dessus du clavier virtuel (13.5)
  initPwa($("install-btn"));                           // installation en application (13.6)
  $("logout").addEventListener("click", logout);
  init();
});
