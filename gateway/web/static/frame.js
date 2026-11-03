// Page dans le cadre principal (shell.js) : un lien vers une autre page est demandé au cadre au lieu de recharger
// celle-ci, pour que la requête en cours continue. Page ouverte directement (sans cadre) : retour au cadre principal.
// Lien Marketplace (data-market) : nouvel onglet ouvert par window.open, qui garde le lien avec cet onglet : la
// marketplace y reprend la clé de session (sessionStorage, propre à chaque onglet) et s'ouvre connectée.
(function () {
  const ROUTES = ["/app", "/admin", "/models"];
  if (window.parent === window) {
    location.replace(location.pathname);
    return;
  }
  document.addEventListener("click", (event) => {
    const link = event.target.closest?.("a[href]");
    if (!link || event.defaultPrevented || event.button !== 0) return;
    if (link.hasAttribute("data-market") && !(event.metaKey || event.ctrlKey || event.shiftKey)) {
      event.preventDefault();
      if (!window.open(link.href, "_blank")) window.top.location.href = link.href;   // fenêtre refusée : sur place
      return;
    }
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || link.target || link.hasAttribute("download")) return;
    const url = new URL(link.href);
    if (url.origin !== location.origin || !ROUTES.includes(url.pathname)) return;
    event.preventDefault();
    window.parent.postMessage({ type: "gw-nav", path: url.pathname }, location.origin);
  });
})();
