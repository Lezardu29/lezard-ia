// Panneau « Double authentification » (20.1), clé maître : activation par QR code, codes de secours, sessions
// d'administration ouvertes à distance (révocables). Sur ce PC, la clé maître seule suffit toujours.
import { api, errorMessage } from "./auth.js";

const $ = (id) => document.getElementById(id);
let state = null;
let pending = null;            // action du formulaire de code : "enable" | "recovery" | "disable"
let shownCodes = [];

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function button(text, cls, onClick) {
  const b = el("button", `btn ${cls} btn-inline`, text);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

function message(text, ok = false) {
  const b = $("tf-msg");
  b.hidden = !text;
  b.className = `alert${text ? " show" : ""}${ok ? " ok-box" : ""}`;
  b.textContent = text || "";
}

const when = (ts) => new Date(ts * 1000).toLocaleString("fr-FR", { dateStyle: "medium", timeStyle: "short" });

function device(ua) {
  if (!ua) return "Appareil inconnu";
  const os = /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Windows/.test(ua) ? "Windows"
    : /Mac OS/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "";
  const nav = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome"
    : /Safari\//.test(ua) ? "Safari" : "";
  return [nav, os].filter(Boolean).join(" sur ") || ua.slice(0, 60);
}

const VIA = { totp: "code de l'application", recovery: "code de secours", activation: "activation" };

function askCode(action, label) {
  pending = action;
  $("tf-actions").hidden = true;                    // un seul bouton de validation à la fois
  $("tf-form").hidden = false;
  $("tf-submit").textContent = label;
  $("tf-code").value = "";
  $("tf-code").focus();
}

function hideForm() {
  pending = null;
  $("tf-actions").hidden = false;
  $("tf-form").hidden = true;
  $("tf-setup").hidden = true;
}

function render(d) {
  state = d;
  $("totp-section").hidden = false;
  $("tf-state").textContent = d.enabled ? "Active" : "Désactivée";
  $("tf-state").classList.toggle("ok", d.enabled);
  const hours = Math.round(d.session_hours * 10) / 10;
  $("tf-status").textContent = d.enabled
    ? `Active depuis le ${when(d.enabled_at)}. Hors de ce PC, l'administration demande la clé maître ET un code `
      + `de votre application ; la session dure ${hours} h. ${d.recovery_left} code(s) de secours restant(s). `
      + "Sur ce PC (http://127.0.0.1), la clé maître seule suffit."
    : "La clé maître seule donne aujourd'hui accès à toute l'administration, y compris depuis Internet (tunnel) "
      + "ou le réseau local. Activez la double authentification : volée, la clé maître ne suffira plus.";
  const actions = $("tf-actions");
  actions.replaceChildren();
  if (!d.enabled) {
    actions.append(button("Activer", "btn-primary", setup));
  } else {
    actions.append(button("Nouveaux codes de secours", "btn-ghost", () => askCode("recovery", "Générer")));
    actions.append(button("Désactiver", "btn-ghost", disable));
  }
  const list = $("tf-sessions");
  list.replaceChildren();
  for (const s of d.enabled ? d.sessions : []) {
    const li = el("li", "bridge");
    const text = el("div", "bridge-text");
    const head = el("div", "bridge-head");
    head.append(el("strong", "", device(s.user_agent)), el("span", "pill", s.current ? "cet appareil" : s.ip || "—"));
    text.append(head, el("p", "muted small", `Ouverte le ${when(s.created_at)} (${VIA[s.via] || s.via}) · `
                                             + `expire le ${when(s.expires_at)} · ${s.ip || ""}`));
    li.append(text, button("Fermer", "btn-ghost", () => revoke(s)));
    list.append(li);
  }
  if (d.enabled && d.sessions.length > 1) {
    const li = el("li", "empty");
    li.append(button("Fermer toutes les sessions", "btn-ghost", revokeAll));
    list.append(li);
  }
}

export async function loadTotp() {
  const r = await api("/v1/admin/totp");
  if (r.ok) render(r.data);
}

async function setup() {
  message("");
  const r = await api("/v1/admin/totp/setup", { method: "POST" });
  if (!r.ok) return message(errorMessage(r));
  $("tf-setup").hidden = false;
  $("tf-qr").hidden = !r.data.svg;
  if (r.data.svg) $("tf-qr").src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(r.data.svg)}`;
  $("tf-secret").textContent = r.data.secret.replace(/(.{4})/g, "$1 ").trim();
  askCode("enable", "Activer");
}

function showCodes(codes) {
  shownCodes = codes;
  $("tf-codes").replaceChildren(...codes.map((c) => el("span", "", c)));
  $("tf-recovery").hidden = false;
  $("tf-recovery").classList.add("show");
}

async function submit(e) {
  e.preventDefault();
  const code = $("tf-code").value.trim();
  if (!code) return message("Saisissez le code affiché par l'application.");
  const path = { enable: "/v1/admin/totp/enable", recovery: "/v1/admin/totp/recovery-codes",
                 disable: "/v1/admin/totp/disable" }[pending];
  $("tf-submit").disabled = true;
  const r = await api(path, { method: "POST", body: { code } });
  $("tf-submit").disabled = false;
  if (!r.ok) {
    $("tf-code").select();
    return message(errorMessage(r));
  }
  const action = pending;
  hideForm();
  if (r.data.recovery_codes) showCodes(r.data.recovery_codes);
  message(action === "enable" ? "Double authentification activée." + (r.data.session ? " Cet appareil reste connecté." : "")
    : action === "recovery" ? "Nouveaux codes de secours : les anciens ne fonctionnent plus."
    : "Double authentification désactivée ; les sessions sont fermées.", true);
  loadTotp();
}

async function disable() {
  if (!state.this_pc) return askCode("disable", "Désactiver");     // à distance : un code en plus
  if (!confirm("Désactiver la double authentification ? La clé maître seule suffira de nouveau partout.")) return;
  const r = await api("/v1/admin/totp/disable", { method: "POST" });
  if (!r.ok) return message(errorMessage(r));
  message("Double authentification désactivée ; les sessions sont fermées.", true);
  loadTotp();
}

async function revoke(s) {
  if (s.current && !confirm("Fermer la session de cet appareil ? Le code sera demandé de nouveau.")) return;
  const r = await api(`/v1/admin/sessions/${encodeURIComponent(s.id)}`, { method: "DELETE" });
  if (!r.ok) return message(errorMessage(r));
  if (!s.current) loadTotp();
  else message("Session fermée.", true);
}

async function revokeAll() {
  if (!confirm("Fermer toutes les sessions d'administration, y compris celle-ci ?")) return;
  const r = await api("/v1/admin/sessions", { method: "DELETE" });
  if (!r.ok) return message(errorMessage(r));
  message(`${r.data.revoked} session(s) fermée(s).`, true);
  if (state.this_pc) loadTotp();
}

function codesText() {
  return "Lezard IA — codes de secours de la double authentification\n"
    + `${location.host} · ${new Date().toLocaleString("fr-FR")}\nChaque code ne sert qu'une fois.\n\n`
    + shownCodes.join("\n") + "\n";
}

export function initTotp() {
  $("tf-form").addEventListener("submit", submit);
  $("tf-cancel").addEventListener("click", () => { hideForm(); message(""); });
  $("tf-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(codesText()); message("Codes copiés.", true); }
    catch { message("Copie impossible : sélectionnez les codes à la main."); }
  });
  $("tf-download").addEventListener("click", () => {
    const url = URL.createObjectURL(new Blob([codesText()], { type: "text/plain" }));
    const a = el("a");
    a.href = url;
    a.download = "codes-secours-gateway.txt";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  $("tf-noted").addEventListener("click", () => {
    shownCodes = [];
    $("tf-codes").replaceChildren();
    $("tf-recovery").hidden = true;
    $("tf-recovery").classList.remove("show");
  });
  loadTotp();
}
