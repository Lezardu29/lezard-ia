// Menus d'actions « ⋯ » (13.7) : <details class="row-menu"> dont le .menu est en position fixed, calculée depuis le
// bouton, pour ne plus être rogné par un conteneur à overflow (.table-wrap). Ouvert vers le haut s'il manque de
// place en bas, recalé dans l'écran, fermé au défilement, au redimensionnement, sur Échap et au clic extérieur.
// Sur petit écran, le menu devient une feuille en bas de l'écran (bottom sheet), sur un fond assombri.
const GAP = 4, MARGIN = 8;
const sheet = matchMedia("(max-width: 640px)");

function place(menu) {
  const box = menu.querySelector(".menu");
  if (!box) return;
  box.style.top = box.style.left = "";
  if (!sheet.matches) {
    const btn = menu.querySelector("summary").getBoundingClientRect();
    const { width, height } = box.getBoundingClientRect();
    const vw = document.documentElement.clientWidth, vh = window.innerHeight;
    let top = btn.bottom + GAP;
    if (top + height > vh - MARGIN && btn.top - GAP - height >= MARGIN) top = btn.top - GAP - height;   // vers le haut
    top = Math.max(MARGIN, Math.min(top, vh - height - MARGIN));
    const edge = menu.classList.contains("align-start") ? btn.left : btn.right - width;   // aligné à droite par défaut
    const left = Math.max(MARGIN, Math.min(edge, vw - width - MARGIN));
    box.style.top = `${top}px`;
    box.style.left = `${left}px`;
  }
  box.dataset.placed = "";
}

function closeAll(except) {
  document.querySelectorAll(".row-menu[open]").forEach((m) => { if (m !== except) m.open = false; });
}

// fond assombri de la bottom sheet : un vrai élément (le ::before d'un <details> n'est pas peint partout)
let backdrop = null;
function syncBackdrop() {
  if (!backdrop) {
    backdrop = document.createElement("div");
    backdrop.className = "menu-backdrop";
    document.body.append(backdrop);
  }
  backdrop.hidden = !(sheet.matches && document.querySelector(".row-menu[open]"));
}

let ready = false;
export function initRowMenus() {
  if (ready) return;                                              // chat + liste des conversations : une seule fois
  ready = true;
  // « toggle » ne remonte pas : écoute en phase de capture
  document.addEventListener("toggle", (e) => {
    const menu = e.target;
    if (!(menu instanceof HTMLDetailsElement) || !menu.classList.contains("row-menu")) return;
    if (menu.open) { closeAll(menu); place(menu); }
    else delete menu.querySelector(".menu")?.dataset.placed;
    syncBackdrop();
  }, true);
  // défilement de la page ou du tableau (capture : les conteneurs défilants ne remontent pas l'événement)
  document.addEventListener("scroll", (e) => { if (!e.target.closest?.(".row-menu .menu")) closeAll(); },
                            { capture: true, passive: true });
  window.addEventListener("resize", () => closeAll());
  sheet.addEventListener("change", () => closeAll());
  document.addEventListener("keydown", (e) => {
    const open = document.querySelector(".row-menu[open]");
    if (e.key !== "Escape" || !open) return;
    open.open = false;
    open.querySelector("summary").focus();
  });
  document.addEventListener("click", (e) => {
    closeAll(e.target.closest(".row-menu") || undefined);         // clic sur le fond ou ailleurs : fermeture
    syncBackdrop();             // un menu retiré de la page à peine fermé (Réessayer) ne signale plus sa fermeture
  });
}
