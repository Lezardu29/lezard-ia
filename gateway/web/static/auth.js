// Gestion de la session côté navigateur (5.1).
// La clé est gardée en sessionStorage (fermée avec l'onglet) ou en localStorage si
// « Se souvenir de moi » est coché. Tout accès au stockage est protégé (navigation privée…).
const STORAGE_KEY = "gw.apiKey";

function store(kind) {
  try { return kind === "local" ? window.localStorage : window.sessionStorage; } catch { return null; }
}

export function getKey() {
  for (const kind of ["session", "local"]) {
    try { const v = store(kind)?.getItem(STORAGE_KEY); if (v) return v; } catch { /* ignoré */ }
  }
  return null;
}

export function saveKey(key, remember) {
  clearKey();
  try { store(remember ? "local" : "session")?.setItem(STORAGE_KEY, key); } catch { /* ignoré */ }
}

export function clearKey() {
  for (const kind of ["session", "local"]) {
    try { store(kind)?.removeItem(STORAGE_KEY); } catch { /* ignoré */ }
  }
}

// Double authentification (20.1) : à distance, une route d'administration répond « session requise » ; on demande
// alors le code (une fois à la fois, toutes pages confondues) et on rejoue la requête. Refusé une fois : on ne
// redemande plus sur cette page (rafraîchissements automatiques), sauf appel explicite à unlockAdmin().
let unlocking = null;
let declined = false;

export function unlockAdmin(key = getKey()) {
  unlocking ||= import("./totp-prompt.js")
    .then((m) => m.askAdminCode(key))
    .then((ok) => { declined = !ok; return ok; })
    .finally(() => { unlocking = null; });
  return unlocking;
}

// Appel API authentifié ; renvoie { ok, status, data } sans jamais lever pour une erreur HTTP.
export async function api(path, options = {}) {
  const r = await rawApi(path, options);
  if (r.status === 401 && r.data?.error?.provider === "admin_session" && !declined
      && await unlockAdmin(options.key === undefined ? getKey() : options.key)) {
    return rawApi(path, options);
  }
  return r;
}

export async function rawApi(path, { key = getKey(), method = "GET", body } = {}) {
  const headers = {};
  if (key) headers.Authorization = `Bearer ${key}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    return { ok: false, status: 0, data: { error: { message: "Gateway injoignable. Vérifiez votre connexion." } } };
  }
  let data = null;
  try { data = await res.json(); } catch { /* corps vide */ }
  return { ok: res.ok, status: res.status, data };
}

export function errorMessage(result) {
  const msg = result?.data?.error?.message;
  if (msg) return msg;
  if (result.status === 401) return "Clé invalide.";
  if (result.status === 429) return "Trop de tentatives, réessayez plus tard.";
  return `Erreur inattendue (${result.status || "réseau"}).`;
}
