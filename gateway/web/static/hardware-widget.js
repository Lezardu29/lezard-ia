// Widget « Matériel & IA locales » (13.3) : jauges VRAM / RAM / CPU, modèles chargés, installation du modèle
// recommandé. Pages d'administration seulement, jamais dans le chat (14.3) : les routes exigent la clé maître.
import { api, errorMessage } from "./auth.js";
import { fmtBytes, pullModel, pulling } from "./ollama-pull.js";

const PERIOD_MS = 5000;
const USE_ORDER = ["chat", "code", "vision", "reasoning"];

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

function gaugeBlock(label) {
  const block = el("div", "gauge-block");
  const g = el("div", "gauge");
  g.setAttribute("role", "meter");
  g.setAttribute("aria-label", label);
  g.setAttribute("aria-valuemin", "0");
  g.setAttribute("aria-valuemax", "100");
  g.append(el("span"));
  const value = el("span", "gauge-value", "…");
  block.append(el("span", "gauge-label", label), g, value);
  return {
    block,
    set(pct, text) {
      g.hidden = pct == null;
      if (pct != null) {
        const p = Math.max(0, Math.min(100, pct));
        g.style.setProperty("--pct", `${p}%`);
        g.classList.toggle("crit", p >= 90);
        g.classList.toggle("warn", p >= 70 && p < 90);
        g.setAttribute("aria-valuenow", String(Math.round(p)));
      }
      value.textContent = text;
    },
  };
}

function expiresIn(iso) {
  const t = Date.parse(iso || "");
  if (!t) return "";
  const min = Math.round((t - Date.now()) / 60000);
  return min > 0 ? ` · déchargé dans ${min} min` : "";
}

export function initHardwareWidget(section, { onInstalled = () => {}, onAddOllama = null } = {}) {
  const head = el("div", "panel-head");
  const title = el("h2", "", "Matériel & IA locales");
  title.id = "hw-title";
  head.append(title, el("span", "muted small", "actualisé toutes les 5 s"));
  const gauges = el("div", "demo-gauges hw-gauges");
  const note = el("p", "muted small hw-note");
  const loadedTitle = el("h3", "hw-sub", "Modèles chargés en mémoire");
  const loaded = el("ul", "hw-loaded");
  const reco = el("div", "hw-reco");
  section.setAttribute("aria-labelledby", "hw-title");
  section.replaceChildren(head, gauges, note, loadedTitle, loaded, reco);

  const gpuGauges = new Map();
  const ram = gaugeBlock("RAM"), cpu = gaugeBlock("Processeur");
  let busy = false, timer = null, stopped = false;

  function renderHardware(h) {
    for (const g of h.gpus) {
      if (!gpuGauges.has(g.index)) {
        const gb = gaugeBlock(h.gpus.length > 1 ? `VRAM · GPU ${g.index}` : "VRAM");
        gpuGauges.set(g.index, gb);
        gauges.append(gb.block);
      }
      const models = h.gpus.length === 1 && h.models_vram ? ` · modèles ${fmtBytes(h.models_vram)}` : "";
      gpuGauges.get(g.index).set(g.vram_total ? (g.vram_used / g.vram_total) * 100 : null,
        `${fmtBytes(g.vram_used)} / ${fmtBytes(g.vram_total)}${models} — ${g.name}`);
    }
    if (!ram.block.isConnected) gauges.append(ram.block, cpu.block);
    ram.set(h.ram.total && h.ram.used != null ? (h.ram.used / h.ram.total) * 100 : null,
      h.ram.total ? `${fmtBytes(h.ram.used)} / ${fmtBytes(h.ram.total)}` : "indisponible");
    const cores = h.cpu.cores_logical ? `${h.cpu.cores_logical} cœurs logiques` : "";
    cpu.set(h.cpu.usage_pct, h.cpu.usage_pct != null ? `${Math.round(h.cpu.usage_pct)} % · ${cores}`
      : `${cores}${cores ? " · " : ""}charge indisponible (psutil non installé)`);

    note.textContent = h.gpus.length ? ""
      : h.gpu_error ? `GPU NVIDIA non lu : ${h.gpu_error}`
      : "Aucun GPU NVIDIA détecté : les modèles locaux tournent sur le processeur et la RAM.";
    note.hidden = !note.textContent;

    loaded.replaceChildren(...h.loaded_models.map((m) => {
      const li = el("li", "hw-model");
      const ramPart = m.size && m.size_vram != null && m.size > m.size_vram ? ` + ${fmtBytes(m.size - m.size_vram)} en RAM` : "";
      const where = m.this_machine ? "" : ` · sur ${m.provider} (autre machine)`;
      li.append(el("span", "", m.model),
                el("span", "muted small", `${fmtBytes(m.size_vram)} en VRAM${ramPart}${where}${expiresIn(m.expires_at)}`));
      return li;
    }));
    if (!h.loaded_models.length) loaded.append(el("li", "empty", "Aucun modèle chargé : Ollama le charge à la première requête."));
  }

  async function refresh() {
    if (busy || stopped || document.hidden) return;
    busy = true;
    const r = await api("/v1/admin/hardware");
    busy = false;
    if (r.status === 401 || r.status === 403) { stop(); return; }
    if (!r.ok) { note.hidden = false; note.textContent = errorMessage(r); return; }
    section.hidden = false;
    renderHardware(r.data);
  }

  async function loadReco() {
    const r = await api("/v1/admin/models/recommendations");
    if (!r.ok) { reco.replaceChildren(); return; }
    const data = r.data, byName = new Map(data.models.map((m) => [m.model, m]));
    const candidates = USE_ORDER.map((u) => [u, byName.get(data.best[u])])
      .filter(([, m]) => m && !m.installed && m.fit !== "too_big");
    const [use, m] = candidates.find(([, x]) => x.fit === "vram") || candidates[0] || [];
    if (!m) {
      reco.replaceChildren(el("p", "muted small", Object.values(data.best).some(Boolean)
        ? "Les modèles recommandés pour ce PC sont installés." : "Aucun modèle du catalogue ne tient sur ce PC."));
      return;
    }
    const text = el("p", "small", `Recommandé pour ${data.uses[use].toLowerCase()} : ${m.label} (${m.model}, ` +
      `${fmtBytes(m.download)}) — ${m.fit === "vram" ? "tient en VRAM" : "VRAM + RAM, plus lent"}.`);
    const status = el("span", "muted small");
    status.setAttribute("aria-live", "polite");
    const b = el("button", "btn btn-primary btn-inline", "Installer le modèle recommandé");
    b.type = "button";
    const row = el("div", "hw-reco-row");
    if (!data.ollama && data.ollama_running) {
      // Ollama tourne sur ce PC mais n'est pas encore ajouté à la gateway : on passe par la fenêtre d'ajout
      status.textContent = "Ollama tourne sur ce PC mais n'est pas encore ajouté : ajoutez-le pour installer ce modèle.";
      if (onAddOllama) {
        b.textContent = "Ajouter Ollama…";
        b.addEventListener("click", onAddOllama);
        row.append(b);
      } else {
        const a = el("a", "btn btn-primary btn-inline", "Ajouter Ollama (page Modèles)");
        a.href = "/models";
        row.append(a);
      }
      row.append(status);
      reco.replaceChildren(text, row);
      return;
    }
    if (!data.ollama) {
      b.disabled = true;
      status.textContent = data.ollama_installed ? "Ollama est installé mais arrêté : démarrez-le pour l'installer."
        : "Ollama n'est pas installé sur ce PC (ollama.com).";
    } else if (!m.disk_ok) { b.disabled = true; status.textContent = "Espace disque insuffisant."; }
    b.addEventListener("click", async () => {
      if (pulling()) { status.textContent = "Un téléchargement est déjà en cours."; return; }
      if (!confirm(`Télécharger ${m.label} (${m.model}) ?\n\n${fmtBytes(m.download)} à télécharger, ` +
                   `${fmtBytes(data.budget.disk_free)} libres sur le disque.`)) return;
      b.disabled = true;
      status.textContent = "Connexion à Ollama…";
      const failed = await pullModel(m.model, data.ollama, (ev) => {
        status.textContent = ev.pct !== undefined ? `Téléchargement : ${ev.pct} %` : ev.status === "done" ? "Terminé" : ev.status;
      });
      if (failed) { b.disabled = false; status.textContent = `Échec : ${failed}`; return; }
      onInstalled(m.model);
      loadReco();
    });
    row.append(b, status);
    reco.replaceChildren(text, row);
  }

  function stop() {
    stopped = true;
    clearInterval(timer);
    section.hidden = true;
  }

  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });   // reprise immédiate
  timer = setInterval(refresh, PERIOD_MS);      // en pause tant que l'onglet est caché (refresh l'ignore)
  refresh();
  loadReco();
  return { refresh, loadReco };
}
