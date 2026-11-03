// Panneau « Accès distant » (5.6), réservé à la clé maître : démarre/arrête un tunnel
// Cloudflare, ngrok ou Tailscale et affiche l'URL publique de l'instance.
import { api, errorMessage } from "./auth.js";

const LABELS = { cloudflare: "Cloudflare Tunnel", ngrok: "ngrok", tailscale: "Tailscale" };
const STATES = { stopped: "Arrêté", starting: "Démarrage…", running: "Actif", reconnecting: "Reconnexion…", error: "Erreur" };
const STABLE_HELP = {
  cloudflare: "Adresse temporaire : elle change à chaque redémarrage du tunnel. Pour une adresse fixe, créez un tunnel nommé " +
    "Cloudflare et renseignez CLOUDFLARE_TUNNEL_TOKEN et CLOUDFLARE_TUNNEL_HOSTNAME dans .env.",
  ngrok: "Adresse temporaire : elle change à chaque redémarrage du tunnel. Pour une adresse fixe, réservez un domaine " +
    "ngrok (gratuit) et renseignez NGROK_DOMAIN dans .env.",
};
let panel, button, poll = null, status = null;

const $p = (sel) => panel.querySelector(sel);

function build() {
  panel = document.createElement("div");
  panel.className = "theme-panel remote-panel";
  panel.id = "remote-panel";
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-labelledby", "remote-title");
  panel.hidden = true;
  panel.innerHTML = `
    <div class="theme-head"><h2 id="remote-title">Accès distant</h2>
      <button type="button" class="icon-btn" data-close aria-label="Fermer">✕</button></div>
    <p class="remote-state"><span class="dot"></span><span data-state></span></p>
    <div class="remote-url" hidden>
      <a data-url target="_blank" rel="noopener noreferrer"></a>
      <button type="button" class="btn btn-ghost btn-inline" data-copy>Copier</button>
    </div>
    <p class="theme-status remote-stable" data-stable hidden></p>
    <div class="remote-lan" data-lan hidden>
      <h3>Réseau local</h3>
      <p class="theme-status">Joignable par les appareils de votre réseau (clé requise) :</p>
      <ul class="lan-urls" data-lan-urls></ul>
      <p class="theme-status" data-lan-tls></p>
    </div>
    <p class="alert" data-error role="alert"></p>
    <div data-form>
      <h3>Fournisseur</h3>
      <div class="providers" role="radiogroup" aria-label="Fournisseur de tunnel"></div>
      <div data-ts-mode hidden>
        <h3>Visibilité Tailscale</h3>
        <div class="segmented" role="radiogroup" aria-label="Visibilité Tailscale">
          <button type="button" role="radio" data-mode="serve" aria-checked="true">Privé (tailnet)</button>
          <button type="button" role="radio" data-mode="funnel" aria-checked="false">Public (funnel)</button>
        </div>
      </div>
      <label class="check anon" data-anon hidden><input type="checkbox" data-anon-box>
        Autoriser l'accès sans clé à travers le tunnel (déconseillé : n'importe qui avec l'adresse
        utiliserait vos modèles et vos crédits)</label>
    </div>
    <div class="remote-security" data-security hidden>
      <h3>Tentatives de connexion</h3>
      <p class="theme-status" data-auth-summary></p>
      <ul class="blocked-ips" data-blocked></ul>
    </div>
    <div class="theme-foot">
      <p class="theme-status" data-port></p>
      <button type="button" class="btn btn-primary btn-inline" data-toggle>Démarrer</button>
    </div>
    <details class="remote-logs"><summary>Journal du tunnel</summary><pre data-logs></pre></details>`;
  document.body.appendChild(panel);

  $p("[data-close]").addEventListener("click", close);
  $p("[data-toggle]").addEventListener("click", toggleTunnel);
  $p("[data-copy]").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(status.public_url); $p("[data-copy]").textContent = "Copié ✓"; }
    catch { $p("[data-copy]").textContent = "Copie impossible"; }
    setTimeout(() => { $p("[data-copy]").textContent = "Copier"; }, 1500);
  });
  panel.querySelectorAll("[data-mode]").forEach((b) => b.addEventListener("click", () => {
    panel.querySelectorAll("[data-mode]").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
    renderForm();
  }));
}

const selectedProvider = () => panel.querySelector(".providers [aria-checked='true']")?.dataset.provider;
const selectedMode = () => panel.querySelector("[data-mode][aria-checked='true']")?.dataset.mode || "serve";

function renderForm() {
  $p("[data-ts-mode]").hidden = selectedProvider() !== "tailscale";
  // tunnel actif = clé requise ; la case n'a de sens que si l'instance accepte l'accès sans clé en local
  $p("[data-anon]").hidden = !(status && !status.require_key);
}

function render() {
  if (!status) return;
  const running = ["running", "starting", "reconnecting"].includes(status.state);
  panel.dataset.state = status.state;
  $p("[data-state]").textContent = `${STATES[status.state] || status.state}` +
    (status.provider && running ? ` · ${LABELS[status.provider]}${status.mode === "private" ? " (privé)" : " (public)"}` +
      (status.anonymous ? " · SANS clé" : " · clé requise") : "") +
    (status.state === "reconnecting" && status.next_retry_at
      ? ` · nouvel essai dans ${Math.max(0, Math.round(status.next_retry_at - Date.now() / 1000))} s` : "") +
    (running && status.restarts ? ` · ${status.restarts} redémarrage(s) automatique(s)` : "");
  const help = running && !status.stable_url ? STABLE_HELP[status.provider] : "";
  $p("[data-stable]").hidden = !help;
  $p("[data-stable]").textContent = help || "";
  $p(".remote-url").hidden = !status.public_url;
  if (status.public_url) {
    $p("[data-url]").textContent = status.public_url;
    $p("[data-url]").href = status.public_url;
  }
  $p("[data-error]").textContent = status.error || "";
  $p("[data-error]").classList.toggle("show", !!status.error);
  $p("[data-form]").hidden = running;
  $p("[data-toggle]").textContent = running ? "Arrêter" : "Démarrer";
  $p("[data-port]").textContent = `Expose le port local ${status.local_port}`;
  $p("[data-logs]").textContent = (status.logs || []).join("\n") || "(vide)";
  renderSecurity(status.auth);
  const lan = status.lan || { enabled: false, urls: [] };
  $p("[data-lan]").hidden = !lan.enabled;
  $p("[data-lan-urls]").replaceChildren(...lan.urls.map((url) => {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = url;
    a.textContent = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    li.append(a);
    return li;
  }));
  const tls = lan.tls || {};                           // 12.7 : chiffrement sur le réseau local
  $p("[data-lan-tls]").className = `theme-status${tls.enabled ? "" : " lan-insecure"}`;
  $p("[data-lan-tls]").textContent = tls.enabled
    ? `Chiffré (HTTPS). Sur chaque appareil, installez une fois le certificat de la gateway (<adresse>/ca.crt) ` +
      `et vérifiez son empreinte SHA-256 : ${tls.fingerprint}`
    : "Non chiffré (HTTP) : la clé et les conversations sont lisibles par les autres appareils du réseau. " +
      "Relancez avec lancer.ps1 -Lan -Https, ou utilisez Tailscale.";

  const box = $p(".providers");
  if (!box.children.length) {
    for (const [id, label] of Object.entries(LABELS)) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "preset";
      b.setAttribute("role", "radio");
      b.dataset.provider = id;
      b.textContent = status.available[id] ? label : `${label} — non installé`;
      b.disabled = !status.available[id];
      b.addEventListener("click", () => {
        box.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
        renderForm();
      });
      box.appendChild(b);
    }
    const first = [...box.children].find((b) => !b.disabled);
    first?.setAttribute("aria-checked", "true");
    if (!first) $p("[data-toggle]").disabled = true;
  }
  renderForm();
}

// anti force brute (12.3) : IP bloquées, avec déblocage manuel
function renderSecurity(auth) {
  $p("[data-security]").hidden = !auth;
  if (!auth) return;
  const min = (s) => Math.round(s / 60);
  $p("[data-auth-summary]").textContent = `${auth.failures_total} identifiant(s) invalide(s) reçu(s) · ` +
    `blocage après ${auth.max_failures} échecs en ${min(auth.window_s)} min, pendant ${min(auth.block_s)} min.`;
  const list = $p("[data-blocked]");
  list.replaceChildren(...auth.blocked.map(({ ip, until }) => {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = `${ip} — bloquée jusqu'à ${new Date(until * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}`;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn btn-ghost btn-inline";
    b.textContent = "Débloquer";
    b.addEventListener("click", async () => {
      b.disabled = true;
      await api(`/v1/admin/security/blocks/${encodeURIComponent(ip)}`, { method: "DELETE" });
      await refresh();
    });
    li.append(label, b);
    return li;
  }));
  if (!auth.blocked.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "Aucune adresse bloquée.";
    list.append(li);
  }
}

async function refresh() {
  const r = await api("/v1/admin/remote");
  if (r.ok) { status = r.data; render(); }
}

async function toggleTunnel() {
  const btn = $p("[data-toggle]");
  btn.disabled = true;
  const running = status && ["running", "starting", "reconnecting"].includes(status.state);
  const r = running
    ? await api("/v1/admin/remote/stop", { method: "POST" })
    : await api("/v1/admin/remote/start", { method: "POST", body: {
        provider: selectedProvider(), mode: selectedMode(), allow_anonymous: !!$p("[data-anon-box]").checked } });
  btn.disabled = false;
  if (!r.ok) {
    status = { ...status, error: errorMessage(r) };
    render();
    return;
  }
  await refresh();
}

function open() {
  panel.hidden = false;
  button.setAttribute("aria-expanded", "true");
  refresh();
  poll = setInterval(refresh, 4000);           // suit l'état du tunnel tant que le panneau est ouvert
  $p("[data-toggle]").focus();
}
function close() {
  panel.hidden = true;
  button.setAttribute("aria-expanded", "false");
  clearInterval(poll);
  button.focus();
}

export function initRemotePanel(trigger) {
  button = trigger;
  button.hidden = false;
  build();
  button.setAttribute("aria-controls", "remote-panel");
  button.setAttribute("aria-expanded", "false");
  button.addEventListener("click", () => (panel.hidden ? open() : close()));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) close(); });
  document.addEventListener("mousedown", (e) => {
    if (!panel.hidden && !panel.contains(e.target) && !button.contains(e.target)) close();
  });
}
