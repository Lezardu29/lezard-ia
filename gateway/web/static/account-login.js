// Connexion avec un compte lezard-ia.fr, depuis la page de connexion de ce PC. Même chiffrement que le site
// (site/assets/compte.js) : le mot de passe donne, par PBKDF2-SHA256 (600 000 tours, sel du compte), la preuve envoyée
// et la clé AES-GCM du coffre, qui ne quitte jamais cette page. La gateway relaie seulement « sel » et « connexion »
// (/v1/compte/…) ; elle ne voit ni le mot de passe ni le contenu du coffre.
// Le coffre liste les machines du compte { name, url, key } : la bonne est celle de cette adresse, sinon celle dont
// la clé est acceptée par ce PC.
import { rawApi } from "./auth.js";

const ITER_MIN = 600000;
const enc = new TextEncoder(), dec = new TextDecoder();

function b64(bytes) {
  let s = "";
  new Uint8Array(bytes).forEach((b) => { s += String.fromCharCode(b); });
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function unb64(text) {
  const s = atob(String(text).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}

async function relay(action, body) {
  let r;
  try {
    r = await fetch(`/v1/compte/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  } catch {
    throw new Error("Lezard IA ne répond pas : vérifiez qu'il est lancé.");
  }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || "Service des comptes indisponible, réessayez plus tard.");
  return j;
}

async function derive(password, salt, iter) {
  const base = await crypto.subtle.importKey("raw", enc.encode(String(password).normalize("NFC")), "PBKDF2", false, ["deriveBits"]);
  const bits = new Uint8Array(await crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt: unb64(salt), iterations: Math.max(ITER_MIN, iter || 0) }, base, 512));
  return { auth: b64(bits.slice(0, 32)), key: await crypto.subtle.importKey("raw", bits.slice(32), "AES-GCM", false, ["decrypt"]) };
}

/** Machines du coffre : { name, url, key } (ancien format { url, key } : une machine). */
function machinesOf(v) {
  const list = v && Array.isArray(v.machines) ? v.machines : v && v.url ? [v] : [];
  return list.slice(0, 10).map((m) => ({ name: String(m?.name || ""), url: String(m?.url || ""), key: String(m?.key || "") }))
    .filter((m) => m.key);
}

/** Identifiant + mot de passe → les machines du compte (déchiffrées ici). */
export async function accountMachines(rawUser, password) {
  if (!crypto?.subtle) throw new Error("Connexion avec un compte impossible sur cette adresse non sécurisée : utilisez http://localhost ou l'adresse https de ce PC.");
  const user = String(rawUser || "").trim().toLowerCase();
  const s = await relay("sel", { user });
  const d = await derive(password, s.salt, s.iter);
  const r = await relay("connexion", { user, auth: d.auth });
  let vault;
  try {
    const data = await crypto.subtle.decrypt({ name: "AES-GCM", iv: unb64(r.vault.iv), additionalData: enc.encode(user) }, d.key, unb64(r.vault.data));
    vault = JSON.parse(dec.decode(data));
  } catch {
    vault = null;
  }
  return machinesOf(vault);
}

const originOf = (url) => { try { return new URL(url).origin; } catch { return ""; } };

/** La clé de ce PC parmi les machines du compte : celle de cette adresse d'abord, sinon celle que ce PC accepte. */
export async function keyForThisPc(machines) {
  const here = machines.filter((m) => originOf(m.url) === location.origin);
  for (const m of [...here, ...machines.filter((m) => !here.includes(m))]) {
    const r = await rawApi("/v1/me", { key: m.key });   // sans demander de code : on cherche seulement la bonne clé
    if (r.ok) return m.key;
    if (r.status === 401 && r.data?.error?.provider === "admin_session") return m.key;   // la bonne : code demandé ensuite
  }
  return null;
}
