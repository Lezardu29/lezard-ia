// Aide des raccourcis clavier du chat (comme celle de Claude) : Ctrl+/ (⌘+/ sur Mac) l'ouvre, Échap la ferme.
// Ctrl+K et Ctrl+Maj+O n'existent que dans l'interface Claude (sidebar.js) : listés seulement là.
const mac = /Mac|iPhone|iPad/.test(navigator.platform);
const MOD = mac ? "⌘" : "Ctrl";
const SHIFT = mac ? "⇧" : "Maj";

function rows() {
  const claude = window.GatewayTheme?.ui?.() === "claude";
  return [
    [["Entrée"], "Envoyer le message"],
    [[SHIFT, "Entrée"], "Aller à la ligne"],
    [["Échap"], "Arrêter la réponse en cours"],
    [["/"], "Commandes en début de message (/aide pour la liste)"],
    ...(claude ? [[[MOD, "K"], "Rechercher dans les conversations"],
                  [[MOD, SHIFT, "O"], "Nouvelle conversation"]] : []),
    [[MOD, "/"], "Afficher cette aide"],
  ];
}

let dialog = null;

function build() {
  dialog = document.createElement("dialog");
  dialog.className = "project-dialog shortcuts-dialog";
  dialog.setAttribute("aria-labelledby", "sc-title");
  const title = Object.assign(document.createElement("h2"), { id: "sc-title", textContent: "Raccourcis clavier" });
  const list = document.createElement("dl");
  list.className = "shortcuts";
  const close = Object.assign(document.createElement("button"), { type: "button", className: "btn btn-ghost btn-inline",
    textContent: "Fermer" });
  close.addEventListener("click", () => dialog.close());
  const actions = document.createElement("div");
  actions.className = "pd-actions";
  actions.append(close);
  dialog.append(title, list, actions);
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });   // clic sur le fond
  document.body.append(dialog);
}

export function openShortcuts() {
  if (!dialog) build();
  dialog.querySelector(".shortcuts").replaceChildren(...rows().flatMap(([keys, text]) => {
    const dt = document.createElement("dt");
    dt.append(...keys.map((k) => Object.assign(document.createElement("kbd"), { textContent: k })));
    return [dt, Object.assign(document.createElement("dd"), { textContent: text })];
  }));
  if (!dialog.open) dialog.showModal();
}

export function initShortcutsHelp() {
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === "/" || e.code === "Slash")) {
      e.preventDefault();
      openShortcuts();
    }
  });
}
