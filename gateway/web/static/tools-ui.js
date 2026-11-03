// Section « Brancher un outil » (16.4) : adresse de la gateway + clé collée par l'utilisateur -> exemples prêts à
// copier. Les clés ne sont jamais relisibles après leur création : celle collée ici reste dans la page.
// Toutes les sessions : sans gestion des clés (clé virtuelle, bac à sable), la clé de la session peut servir et
// seule l'adresse actuelle est proposée (la liste complète révèle les adresses internes du PC).
import { api, getKey } from "./auth.js";
import { mountGuides } from "./integrations.js";

const $ = (id) => document.getElementById(id);
const NOTES = {
  local: "Adresse de ce PC seulement : pour un autre appareil, activez l'accès distant ou le mode réseau local.",
  lan: "Appareils du même réseau (Wi-Fi / box) que ce PC.",
  tailscale: "Appareils connectés à votre réseau Tailscale.",
  internet: "Joignable de partout.",
};

export async function initTools({ ownKey = false } = {}) {
  const asked = /^#guide-([a-z0-9]+)$/.exec(location.hash)?.[1];   // « Guide de branchement » d'une fiche de la marketplace
  const update = mountGuides($("tl-guides"), asked ? { initial: asked } : {});
  if (ownKey) {
    $("tl-intro").textContent = "Open WebUI, Continue, Cursor, scripts… : tout outil compatible OpenAI se branche sur " +
      "la gateway. Collez la clé de l'outil, ou utilisez celle de cette session : les exemples se remplissent. La clé " +
      "reste dans cette page, elle n'est envoyée nulle part.";
    $("tl-own").hidden = !getKey();
    $("tl-own").addEventListener("click", () => {
      $("tl-key").value = getKey() || "";
      $("tl-key").dispatchEvent(new Event("input"));
    });
  }
  const r = ownKey ? { ok: false } : await api("/v1/access");
  const urls = r.ok ? r.data.urls || [] : [{ kind: "current", url: location.origin, label: "Adresse actuelle" }];
  const sel = $("tl-url");
  sel.length = 0;
  for (const u of urls) sel.add(new Option(`${u.label} — ${u.url}`, u.url));
  const render = () => {
    const u = urls.find((x) => x.url === sel.value) || urls[0];
    if (!u) return;
    const note = NOTES[u.kind] || "";
    $("tl-note").textContent = note;
    $("tl-note").hidden = !note;
    update(u.url, $("tl-key").value.trim() || "VOTRE_CLE", u.kind);
  };
  sel.addEventListener("change", render);
  $("tl-key").addEventListener("input", render);
  $("tools-section").hidden = false;
  render();
  if (asked) $("tools-section").scrollIntoView({ block: "start" });
}
