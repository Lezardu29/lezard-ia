// Panneau « Version et mises à jour » (18.3), clé maître : version installée, nouveautés disponibles. Copie git :
// la mise à jour se lance sur le PC (lancer.ps1 -Update : sauvegarde, tests, retour arrière). Installation Windows
// (32.3) : version signée, installée d'ici après vérification, ou retour à la version précédente.
import { api, errorMessage } from "./auth.js";

const $ = (id) => document.getElementById(id);
const when = (iso) => new Date(iso).toLocaleString("fr-FR", { dateStyle: "medium", timeStyle: "short" });
const whenTs = (ts) => new Date(ts * 1000).toLocaleString("fr-FR", { dateStyle: "medium", timeStyle: "short" });

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function render(d) {
  $("update-section").hidden = false;
  const i = d.installed;
  $("up-installed").textContent = `Version ${i.version}` + (i.commit ? ` · ${i.commit}` : "")
    + (i.date ? ` du ${when(i.date)}` : "") + (i.branch ? ` (branche ${i.branch})` : "")
    + (i.local_changes ? " · fichiers modifiés localement" : "");
  const last = d.last;
  const box = $("up-result");
  box.replaceChildren();
  box.className = "alert";
  box.hidden = true;
  if (!d.can_check) {
    $("up-check").hidden = true;
    $("up-status").textContent = "Installation sans git (image Docker ou archive) : mettez à jour l'image "
      + "(docker compose pull puis up -d) ou l'archive.";
    return;
  }
  $("up-status").textContent = (d.enabled ? "Vérification automatique une fois par jour."
    : "Vérification automatique désactivée (GATEWAY_UPDATE_CHECK=on dans .env pour l'activer).")
    + (last ? ` Dernière vérification : ${whenTs(last.at)}.` : "");
  if (!last) return;
  box.hidden = false;
  if (d.can_install && !last.error) return renderRelease(d, box);
  if (last.error) {
    box.className = "alert show warn-box";
    box.textContent = last.error;
  } else if (!last.available) {
    box.className = "alert show ok-box";
    box.textContent = `À jour : aucune nouveauté sur ${last.upstream}.`;
  } else {
    box.className = "alert show warn-box";
    const wrap = el("div");
    wrap.append(el("p", "", `${last.behind} nouveauté(s) disponible(s) sur ${last.upstream} :`));
    const ul = el("ul", "up-commits");
    for (const c of last.commits || []) {
      const li = el("li");
      li.append(el("code", "", c.commit), ` ${c.subject} `, el("span", "muted small", `(${when(c.date)})`));
      ul.append(li);
    }
    wrap.append(ul);
    const how = el("p", "small");
    how.append("Pour l'installer, sur le PC de la gateway : ", el("code", "", d.command),
               " — sauvegarde de la base, mise à jour, tests, redémarrage ; retour à la version actuelle si un test échoue.");
    wrap.append(how);
    box.append(wrap);
  }
}

function installButton(label, target, version, cls = "btn-primary") {
  const b = el("button", `btn ${cls} btn-inline`, label);
  b.type = "button";
  b.addEventListener("click", async () => {
    const what = target === "latest" ? `la version ${version}` : `la version précédente (${version})`;
    if (!confirm(`Installer ${what} ? La base est sauvegardée d'abord ; Lezard IA s'arrête une minute puis redémarre.`)) return;
    b.disabled = true;
    b.textContent = "Téléchargement et vérification…";
    const r = await api("/v1/admin/update/install", { method: "POST", body: { target } });
    const box = $("up-result");
    box.replaceChildren();
    box.className = `alert show ${r.ok ? "ok-box" : "warn-box"}`;
    box.textContent = r.ok
      ? `Installation de la version ${r.data.installing} lancée (installateur vérifié, base sauvegardée). `
        + "Lezard IA redémarre dans une minute : rechargez la page ensuite."
      : errorMessage(r);
    if (!r.ok) b.disabled = false;
  });
  return b;
}

function renderRelease(d, box) {                      // installation Windows : versions signées (32.3)
  const last = d.last;
  const wrap = el("div");
  const actions = el("div", "panel-actions");
  if (last.available) {
    box.className = "alert show warn-box";
    wrap.append(el("p", "", `Version ${last.version} disponible`
      + (last.size ? ` (${Math.max(1, Math.round(last.size / 1e6))} Mo, signée et vérifiée avant l'installation)` : "") + " :"));
    if (last.notes) wrap.append(el("p", "small", last.notes));
    actions.append(installButton("Installer la mise à jour", "latest", last.version));
  } else {
    box.className = "alert show ok-box";
    wrap.append(el("p", "", `À jour : la version ${d.installed.version} est la plus récente.`));
  }
  if (last.previous) actions.append(installButton("Revenir à la version précédente", "previous", last.previous, "btn-ghost"));
  if (actions.children.length) wrap.append(actions);
  box.replaceChildren(wrap);
}

async function load() {
  const r = await api("/v1/admin/update");
  if (r.ok) render(r.data);
}

async function checkNow() {
  const b = $("up-check");
  b.disabled = true;
  b.textContent = "Vérification…";
  const r = await api("/v1/admin/update/check", { method: "POST" });
  b.disabled = false;
  b.textContent = "Vérifier maintenant";
  if (!r.ok) {
    $("up-result").hidden = false;
    $("up-result").className = "alert show";
    $("up-result").textContent = errorMessage(r);
    return;
  }
  render(r.data);
}

export function initUpdates(me) {
  if (me.update?.available) {                         // badge à côté de la version, dans la barre du haut
    const v = $("app-version");
    v.textContent = `v${me.version} · mise à jour disponible`;
    v.classList.add("update-available");
    v.title = "Une nouvelle version est disponible (section Version et mises à jour)";
  }
  $("up-check").addEventListener("click", checkNow);
  load();
}
