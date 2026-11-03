// Téléchargement d'un modèle Ollama en flux NDJSON (page Modèles, widget matériel 13.3). Clé maître seulement.
import { api, errorMessage, getKey } from "./auth.js";

export const fmtBytes = (n) => n == null ? "?"
  : n >= 1e9 ? `${(n / 1e9).toFixed(1).replace(".", ",")} Go` : `${Math.round(n / 1e6)} Mo`;

let running = false;
export const pulling = () => running;          // un seul téléchargement à la fois, toutes vues confondues

// onProgress({ pct, completed, total }) ou onProgress({ status }) ; renvoie null si réussi, sinon le message d'erreur
export async function pullModel(model, provider, onProgress = () => {}) {
  if (running) return "Un téléchargement est déjà en cours.";
  running = true;
  let failed = null;
  try {
    const res = await fetch("/v1/admin/ollama/pull", {
      method: "POST", headers: { Authorization: `Bearer ${getKey()}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model, provider }),
    });
    if (!res.ok) {
      let data = null;
      try { data = await res.json(); } catch { /* corps vide */ }
      throw new Error(errorMessage({ status: res.status, data }));
    }
    const reader = res.body.getReader(), decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.error) { failed = ev.error; continue; }
        if (ev.total && ev.completed !== undefined) {
          onProgress({ pct: Math.min(100, Math.round((ev.completed / ev.total) * 100)), completed: ev.completed, total: ev.total });
        } else if (ev.status) {
          onProgress({ status: ev.status });
        }
      }
    }
  } catch (err) {
    failed = err.message || "Téléchargement interrompu.";
  } finally {
    running = false;
  }
  return failed;
}

/** Ollama prêt en un clic : installé s'il manque, démarré, ajouté à Lezard IA (tâche de la gateway, suivie ici).
 *  onStep(texte) à chaque étape ; renvoie { provider } ou { error }. */
export async function ensureOllama(onStep = () => {}) {
  let r = await api("/v1/admin/ollama/ready", { method: "POST" });
  if (!r.ok) return { error: errorMessage(r) };
  while (r.data.state === "running") {
    if (r.data.step) onStep(r.data.step);
    await new Promise((ok) => setTimeout(ok, 2000));
    const next = await api("/v1/admin/ollama/ready");
    if (next.ok) r = next;
  }
  return r.data.state === "done" ? { provider: r.data.provider } : { error: r.data.error || "Ollama n'a pas pu être préparé." };
}
