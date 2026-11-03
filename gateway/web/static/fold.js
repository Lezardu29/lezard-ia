// Panneaux repliables du tableau de bord et de la page Modèles : chaque panneau d'un groupe montre un aperçu de même
// hauteur (--fold-height dans admin.css) ; « Afficher plus » le déplie. Le bouton est placé après le panneau : le
// contenu du panneau peut être redessiné par son script sans le perdre.
const MORE = "Afficher plus", LESS = "Afficher moins";

function foldHeight() {
  return parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--fold-height")) || 260;
}

function setup(panel) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn btn-ghost btn-inline fold-toggle";
  btn.textContent = MORE;
  btn.setAttribute("aria-expanded", "false");
  if (panel.id) btn.setAttribute("aria-controls", panel.id);
  panel.classList.add("fold");
  panel.after(btn);

  const setOpen = (open) => {
    panel.classList.toggle("fold-open", open);
    btn.setAttribute("aria-expanded", String(open));
    btn.textContent = open ? LESS : MORE;
  };
  const update = () => {                         // court : ni dégradé ni bouton
    const tall = panel.classList.contains("fold-open") || panel.scrollHeight > foldHeight() + 8;
    panel.classList.toggle("fold-short", !tall);
    btn.hidden = !tall;
  };
  btn.addEventListener("click", () => {
    setOpen(!panel.classList.contains("fold-open"));
    if (!panel.classList.contains("fold-open")) panel.scrollIntoView({ block: "nearest" });
  });
  // clavier : un élément caché sous l'aperçu qui reçoit le focus déplie le panneau
  panel.addEventListener("focusin", (e) => {
    if (panel.classList.contains("fold-open")) return;
    if (e.target.getBoundingClientRect().bottom > panel.getBoundingClientRect().bottom) setOpen(true);
  });
  new MutationObserver(update).observe(panel, { childList: true, subtree: true, characterData: true, attributes: true,
                                                 attributeFilter: ["hidden"] });
  window.addEventListener("resize", update);
  update();
}

export function initFolds() {
  document.querySelectorAll(".dash-group > .panel").forEach(setup);
}
