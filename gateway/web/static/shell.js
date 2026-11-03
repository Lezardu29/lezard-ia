// Cadre principal de Lezard IA. Chaque page est chargée une seule fois, dans son propre cadre, puis gardée : changer de
// page n'envoie plus rien au serveur, et une requête en cours (le chat) continue pendant qu'on regarde le tableau de bord.
const ROUTES = { "/app": "Lezard IA", "/admin": "Tableau de bord · Lezard IA", "/models": "Modèles · Lezard IA" };

// Une page ouverte dans un cadre qui se recharge ailleurs (après une connexion, par exemple) : on la remet à sa place.
if (window.top !== window) {
  location.replace(`${location.pathname}?frame=1`);
} else {
  const stage = document.getElementById("stage");
  const frames = new Map();

  // Déconnexion ou session finie dans un cadre : la page de connexion s'affiche à la place de tout le site.
  function leaveIfLogin(frame) {
    try {
      if (frame.contentWindow.location.pathname === "/") location.replace("/");
    } catch {
      /* cadre pas encore prêt : rien à faire */
    }
  }

  function show(path, push) {
    const route = ROUTES[path] ? path : "/app";
    if (!frames.has(route)) {
      const frame = document.createElement("iframe");
      frame.title = ROUTES[route];
      frame.src = `${route}?frame=1`;
      frame.addEventListener("load", () => leaveIfLogin(frame));
      stage.append(frame);
      frames.set(route, frame);
    }
    for (const [name, frame] of frames) frame.hidden = name !== route;
    document.title = ROUTES[route];
    if (location.pathname !== route) history[push ? "pushState" : "replaceState"]({}, "", route);
  }

  // un lien interne d'une page (dans son cadre) : c'est ce cadre principal qui affiche la page demandée
  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin || event.data?.type !== "gw-nav" || !ROUTES[event.data.path]) return;
    if (![...frames.values()].some((frame) => frame.contentWindow === event.source)) return;
    show(event.data.path, true);
  });

  window.addEventListener("popstate", () => show(location.pathname, false));
  show(location.pathname, false);
}
