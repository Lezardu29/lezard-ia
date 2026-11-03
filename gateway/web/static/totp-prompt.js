// Demande du code de double authentification (20.1), sur toutes les pages : à distance, la clé maître s'échange
// avec ce code contre une session d'administration (cookie HttpOnly, posé par le serveur).
import { errorMessage, rawApi } from "./auth.js";

let dialog = null;

function build() {
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/totp.css";
  document.head.append(link);
  dialog = document.createElement("dialog");
  dialog.id = "totp-prompt";
  dialog.setAttribute("aria-labelledby", "tp-title");
  dialog.innerHTML = `
    <form method="dialog" novalidate>
      <h2 id="tp-title">Double authentification</h2>
      <p class="tp-text">Connexion à distance : saisissez le code à 6 chiffres de votre application
        d'authentification, ou un code de secours.</p>
      <input id="tp-code" name="code" inputmode="numeric" autocomplete="one-time-code" spellcheck="false"
             maxlength="16" placeholder="123456" aria-describedby="tp-error" required>
      <p class="tp-error" id="tp-error" role="alert"></p>
      <div class="tp-actions">
        <button type="button" class="btn btn-ghost btn-inline" value="cancel" id="tp-cancel">Annuler</button>
        <button type="submit" class="btn btn-primary btn-inline" id="tp-ok">Valider</button>
      </div>
      <p class="tp-help">Téléphone perdu, sans code de secours : sur le PC de la gateway, <code>.\\lancer.ps1 -ResetTotp</code>.</p>
    </form>`;
  document.body.append(dialog);
}

export function askAdminCode(key) {
  if (!dialog) build();
  const input = dialog.querySelector("#tp-code");
  const error = dialog.querySelector("#tp-error");
  const ok = dialog.querySelector("#tp-ok");
  input.value = "";
  error.textContent = "";
  return new Promise((resolve) => {
    const done = (value) => {
      dialog.removeEventListener("submit", submit);
      dialog.removeEventListener("cancel", cancel);
      dialog.querySelector("#tp-cancel").removeEventListener("click", cancel);
      if (dialog.open) dialog.close();
      resolve(value);
    };
    const cancel = (e) => { e.preventDefault(); done(false); };
    async function submit(e) {
      e.preventDefault();
      const code = input.value.trim();
      if (!code) { error.textContent = "Saisissez le code."; input.focus(); return; }
      ok.disabled = true;
      const r = await rawApi("/v1/admin/session", { key, method: "POST", body: { code } });
      ok.disabled = false;
      if (!r.ok) {
        error.textContent = errorMessage(r);
        input.select();
        return;
      }
      done(true);
      if (r.data.warning) alert(r.data.warning);          // code de secours consommé
    }
    dialog.addEventListener("submit", submit);
    dialog.addEventListener("cancel", cancel);              // Échap
    dialog.querySelector("#tp-cancel").addEventListener("click", cancel);
    dialog.showModal();
    input.focus();
  });
}
