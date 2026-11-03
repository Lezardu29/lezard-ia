// Interface mobile (13.5) : navigation en tiroir sur écran étroit, zone de saisie visible avec le clavier virtuel.
// Les éléments de la barre du haut marqués data-drawer passent dans le tiroir sous 640 px et reviennent à leur
// place au-delà (mêmes nœuds : leurs écouteurs restent attachés). Page du chat (interface Lezard IA) : sur grand
// écran aussi, ils sont rangés, dans un menu déroulant ouvert par ☰ à droite de la barre (une dizaine d'icônes sinon).
const compact = matchMedia("(max-width: 640px)");
const FOCUSABLE = "a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex='-1'])";
// éléments confiés au menu du compte (interface Claude, sidebar.js) : le tiroir ne les prend plus
const hold = { held: false, place: null };

export function holdDrawer(on) {
  hold.held = on;
  hold.place?.();
}

function el(tag, cls, attrs = {}) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

function initDrawer() {
  const bar = document.querySelector(".topbar");
  const items = bar ? [...bar.querySelectorAll("[data-drawer]")] : [];
  if (!items.length) return;
  const btn = el("button", "icon-btn menu-btn", { type: "button", "aria-label": "Menu", "aria-expanded": "false",
                                                   "aria-controls": "drawer" });
  btn.innerHTML = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg>';
  bar.prepend(btn);
  const drawer = el("aside", "drawer", { id: "drawer", role: "dialog", "aria-modal": "true", "aria-label": "Navigation" });
  const head = el("div", "drawer-head");
  const title = el("strong");
  title.textContent = "Lezard IA";
  const close = el("button", "icon-btn", { type: "button", "aria-label": "Fermer le menu" });
  close.textContent = "✕";
  head.append(title, close);
  const body = el("div", "drawer-body");
  drawer.append(head, body);
  const backdrop = el("div", "drawer-backdrop");
  drawer.hidden = backdrop.hidden = true;
  document.body.append(backdrop, drawer);
  const marks = items.map((item) => {                 // place d'origine de chaque élément
    const m = document.createComment("drawer");
    item.before(m);
    return m;
  });

  const menuPage = document.body.classList.contains("chat-page");
  const asMenu = () => menuPage && !compact.matches;

  function open() {
    if (asMenu()) drawer.style.top = `${Math.round(bar.getBoundingClientRect().bottom) + 6}px`;   // sous la barre
    else drawer.style.top = "";
    drawer.hidden = backdrop.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    document.body.classList.add("drawer-open");
    (body.querySelector(FOCUSABLE) || close).focus();
  }
  function shut(focus = true) {
    if (drawer.hidden) return;
    drawer.hidden = backdrop.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    document.body.classList.remove("drawer-open");
    if (focus) btn.focus();
  }
  function place() {
    btn.hidden = hold.held;
    const menu = asMenu();
    if (drawer.classList.contains("drawer-menu") !== menu) shut(false);
    drawer.classList.toggle("drawer-menu", menu);
    backdrop.classList.toggle("clear", menu);
    if (menu) bar.append(btn); else bar.prepend(btn);  // menu déroulant : à droite, comme un menu de compte
    if (hold.held) {                                 // rendus à leur place : sidebar.js les range ensuite
      shut(false);
      items.forEach((item, i) => { if (item.parentNode === body) marks[i].after(item); });
    } else if (compact.matches || menuPage) body.append(...items);
    else { shut(false); items.forEach((item, i) => marks[i].after(item)); }
  }
  hold.place = place;

  btn.addEventListener("click", open);
  close.addEventListener("click", () => shut());
  backdrop.addEventListener("click", () => shut());
  // une action du tiroir (lien, panneau Apparence, déconnexion…) le referme ; elle a déjà reçu le clic
  body.addEventListener("click", (e) => { if (e.target.closest("a, button")) shut(false); });
  drawer.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); shut(); return; }
    if (e.key !== "Tab") return;                       // focus gardé dans le tiroir
    const f = [...drawer.querySelectorAll(FOCUSABLE)].filter((x) => x.offsetParent !== null);
    if (!f.length) return;
    if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f.at(-1).focus(); }
    else if (!e.shiftKey && document.activeElement === f.at(-1)) { e.preventDefault(); f[0].focus(); }
  });
  compact.addEventListener("change", place);
  place();
}

// Clavier virtuel : la hauteur de l'application suit la zone réellement visible (visualViewport). Chrome Android
// redimensionne déjà la page (interactive-widget=resizes-content) ; Safari iOS non : le clavier recouvrirait la saisie.
function keepComposerVisible(scroller) {
  const vv = window.visualViewport;
  if (!vv) return;
  const root = document.documentElement;
  const apply = () => {
    if (vv.scale > 1.01) { root.style.removeProperty("--app-height"); return; }   // zoom au doigt : rien à faire
    const atBottom = scroller && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
    root.style.setProperty("--app-height", `${Math.round(vv.height)}px`);
    if (vv.offsetTop > 0) window.scrollTo(0, 0);       // iOS fait défiler la page sous le clavier
    if (atBottom) requestAnimationFrame(() => { scroller.scrollTop = scroller.scrollHeight; });
  };
  vv.addEventListener("resize", apply);
  vv.addEventListener("scroll", apply);
  apply();
}

export const isCompact = () => compact.matches;

export function initMobile({ chatScroller = null } = {}) {
  initDrawer();
  if (chatScroller) keepComposerVisible(chatScroller);
}
