// Service worker de l'application installée (13.6).
// Ne met en cache QUE les fichiers statiques (/static/ : styles, scripts, icônes), jamais /v1/ (clés,
// conversations, réponses) ni les pages : une page demandée hors connexion affiche /static/offline.html.
// Servi à la racine (/sw.js) pour couvrir tout le site ; toujours revalidé (Cache-Control: no-cache).
const VERSION = "gw-static-v40"; // v7 : modèle imposé dans le chat
const OFFLINE = "/static/offline.html";
const PRECACHE = [OFFLINE, "/static/offline.js", "/static/styles.css", "/static/device.js", "/static/theme.js", "/static/icons/icon-192.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

function isStatic(request, url) {
  return request.method === "GET" && url.origin === self.location.origin && url.pathname.startsWith("/static/")
    && !request.headers.has("authorization");
}

// réseau d'abord : une mise à jour de la gateway est prise aussitôt ; le cache ne sert que hors connexion
async function networkFirst(request) {
  const cache = await caches.open(VERSION);
  try {
    // no-cache : revalidé à chaque fois (ETag), même si un proxy (Cloudflare) impose un long max-age au navigateur
    const response = await fetch(request, { cache: "no-cache" });
    if (response.ok && response.type === "basic") cache.put(request, response.clone());
    return response;
  } catch {
    return (await cache.match(request)) || Response.error();
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request, url = new URL(request.url);
  if (isStatic(request, url)) {
    event.respondWith(networkFirst(request));
  } else if (request.mode === "navigate" && url.origin === self.location.origin) {
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE)));    // la page elle-même : jamais en cache
  }
  // tout le reste (/v1/…, autres origines) passe sans le service worker
});
