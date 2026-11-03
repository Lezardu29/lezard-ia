// Panneau « Sauvegardes » (18.2), clé maître : sauvegarde immédiate, liste, restauration (au prochain démarrage)
// et export de la clé de chiffrement. Restaurer et exporter : depuis ce PC seulement (le serveur le vérifie).
import { api, errorMessage } from "./auth.js";
import { saveBlob } from "./save-file.js";

const $ = (id) => document.getElementById(id);
let state = null;

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function box(id, text, kind) {
  const b = $(id);
  b.hidden = !text;
  b.className = `alert${text ? " show" : ""}${kind ? ` ${kind}` : ""}`;
  if (text !== undefined && id !== "bk-key") b.textContent = text;
}

const message = (text, ok = false) => box("bk-msg", text, ok ? "ok-box" : "");
const when = (ts) => new Date(ts * 1000).toLocaleString("fr-FR", { dateStyle: "medium", timeStyle: "short" });
const size = (b) => (b >= 1e6 ? `${(b / 1e6).toFixed(1)} Mo` : `${Math.max(1, Math.round(b / 1e3))} ko`);
const LABELS = { auto: "automatique", manuelle: "manuelle", "avant-restauration": "avant restauration" };

function render(d) {
  state = d;
  $("backups-section").hidden = false;
  $("bk-now").disabled = !d.available;
  const last = d.data[0];
  $("bk-status").textContent = !d.available ? "Base en mémoire : aucune sauvegarde."
    : `${d.auto ? `Automatique, une fois par ${d.interval_hours} h` : "Sauvegarde automatique désactivée (GATEWAY_BACKUP=off)"}`
      + ` · automatiques : ${d.keep.daily} jours et ${d.keep.weekly} semaines conservés, autres : les `
      + `${d.keep.other} plus récentes · dossier ${d.directory}`
      + ` · ${last ? `dernière le ${when(last.created_at)}` : "aucune sauvegarde pour l'instant"}.`;
  box("bk-disk", d.same_disk
    ? "Les sauvegardes sont sur le même disque que la base : elles ne survivraient pas à une panne de ce disque. "
      + "Indiquez un autre disque, une clé USB ou un dossier réseau dans GATEWAY_BACKUP_DIR (.env)."
    : "", "warn-box");
  box("bk-pending", d.pending
    ? `Restauration de « ${d.pending.source} » en attente : elle sera appliquée au prochain démarrage de la gateway.`
    : "", "warn-box");
  if (d.pending) {
    if (d.can_restart && d.this_pc) {                // lancée par lancer.ps1 ou le service : relance possible (18.1)
      const now = el("button", "btn btn-primary btn-inline", "Redémarrer maintenant");
      now.type = "button";
      now.addEventListener("click", () => restartGateway(now));
      $("bk-pending").append(" ", now);
    }
    const cancel = el("button", "btn btn-ghost btn-inline", "Annuler");
    cancel.type = "button";
    cancel.addEventListener("click", cancelRestore);
    $("bk-pending").append(" ", cancel);
  }
  renderKey(d);
  const list = $("bk-list");
  list.replaceChildren();
  for (const b of d.data) {
    const li = el("li", "bridge");
    const text = el("div", "bridge-text");
    const head = el("div", "bridge-head");
    head.append(el("strong", "", when(b.created_at)), el("span", "pill", LABELS[b.label] || b.label || "—"));
    text.append(head, el("p", "muted small", `${b.name} · ${size(b.size_bytes)}`));
    li.append(text);
    if (d.this_pc) {
      const btn = el("button", "btn btn-ghost btn-inline", "Restaurer");
      btn.type = "button";
      btn.addEventListener("click", () => restore(b, btn));
      li.append(btn);
    }
    list.append(li);
  }
  if (d.data.length && !d.this_pc) {
    list.append(el("li", "empty", "Restaurer : depuis ce PC seulement (http://127.0.0.1), accès distant arrêté."));
  }
}

function renderKey(d) {
  const exported = d.key.exported && d.key.exported.fingerprint === d.key.fingerprint;
  const text = exported
    ? `Clé de chiffrement exportée le ${when(d.key.exported.at)} (empreinte ${d.key.fingerprint}). Gardez le fichier et sa phrase de passe à part de la base.`
    : "La clé de chiffrement n'est jamais copiée dans les sauvegardes. Sans elle, une sauvegarde restaurée sur un "
      + "disque neuf perd les clés API de vos fournisseurs. Exportez-la une fois, protégée par une phrase de passe, "
      + "et rangez le fichier ailleurs (clé USB, gestionnaire de mots de passe)."
      + (d.this_pc ? "" : " Export possible depuis ce PC seulement.");
  box("bk-key", text, exported ? "" : "warn-box");
  $("bk-key-text").textContent = text;
  $("bk-key-form").hidden = exported || !d.this_pc;
}

export async function loadBackups() {
  const r = await api("/v1/admin/backups");
  if (r.ok) render(r.data);
}

async function backupNow() {
  const button = $("bk-now");
  button.disabled = true;
  const r = await api("/v1/admin/backups", { method: "POST" });
  button.disabled = false;
  if (!r.ok) return message(errorMessage(r));
  message(`Sauvegarde créée : ${r.data.name}.`, true);
  loadBackups();
}

async function restore(b, button) {
  button.disabled = true;
  const r = await api(`/v1/admin/backups/${encodeURIComponent(b.name)}`);
  button.disabled = false;
  if (!r.ok) return message(errorMessage(r));
  if (!r.data.ok) return message(`Restauration impossible : ${r.data.problem}`);
  const lost = r.data.credentials_other_key.length
    ? `\n\nAttention : les clés API de ${r.data.credentials_other_key.join(", ")} ont été chiffrées avec une AUTRE `
      + "clé de chiffrement. Importez-la d'abord (python -m gateway.backup import-key), sinon elles seront illisibles."
    : "";
  if (!confirm(`Restaurer la sauvegarde du ${when(b.created_at)} ?\n\n${r.data.keys} clé(s) virtuelle(s), `
               + `${r.data.credentials} clé(s) de fournisseur. Tout ce qui a changé depuis sera remplacé ; l'état `
               + `actuel est d'abord sauvegardé. La restauration s'applique au prochain démarrage.${lost}`)) return;
  button.disabled = true;
  const s = await api(`/v1/admin/backups/${encodeURIComponent(b.name)}/restore`,
                      { method: "POST", body: { confirm: b.name } });
  button.disabled = false;
  if (!s.ok) return message(errorMessage(s));
  message(state.can_restart
    ? "Restauration prête : cliquez sur « Redémarrer maintenant » pour l'appliquer."
    : "Restauration prête : redémarrez la gateway (Ctrl+C dans sa fenêtre, puis lancer.ps1) pour l'appliquer.", true);
  loadBackups();
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function healthy() {
  try { return (await fetch("/health", { cache: "no-store" })).ok; } catch { return false; }
}

async function restartGateway(button) {
  if (!confirm("Redémarrer la gateway maintenant ? Les conversations en cours sont interrompues quelques secondes.")) return;
  button.disabled = true;
  const r = await api("/v1/admin/restart", { method: "POST" });
  if (!r.ok) { button.disabled = false; return message(errorMessage(r)); }
  message("Redémarrage en cours…");
  // arrêt demandé une demi-seconde après la réponse : attendre la coupure, puis le retour
  for (let i = 0; i < 20 && await healthy(); i++) await pause(250);
  for (let i = 0; i < 120; i++) {
    if (await healthy()) {
      message("Gateway redémarrée.", true);
      return loadBackups();
    }
    await pause(1000);
  }
  message("La gateway ne répond pas après 2 minutes : voir data/logs/gateway.log sur le PC.");
}

async function cancelRestore() {
  const r = await api("/v1/admin/backups/restore", { method: "DELETE" });
  if (!r.ok) return message(errorMessage(r));
  message("Restauration annulée.", true);
  loadBackups();
}

async function exportKey(e) {
  e.preventDefault();
  const pass = $("bk-pass").value;
  if (pass !== $("bk-pass2").value) return message("Les deux phrases de passe diffèrent.");
  const button = $("bk-export");
  button.disabled = true;
  const r = await api("/v1/admin/encryption-key/export", { method: "POST", body: { passphrase: pass } });
  button.disabled = false;
  if (!r.ok) return message(errorMessage(r));
  $("bk-pass").value = $("bk-pass2").value = "";
  await saveBlob(new Blob([JSON.stringify(r.data, null, 2)], { type: "application/json" }),
                 `cle-chiffrement-${r.data.fingerprint}.json`);
  message("Fichier de clé téléchargé. Rangez-le à part et retenez la phrase de passe : elle n'est enregistrée nulle part.",
          true);
  loadBackups();
}

export function initBackups() {
  $("bk-now").addEventListener("click", backupNow);
  $("bk-key-form").addEventListener("submit", exportKey);
  loadBackups();
}
