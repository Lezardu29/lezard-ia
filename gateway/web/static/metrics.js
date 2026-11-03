// Métriques discrètes sous chaque réponse (5.5) : modèle, tokens, latence, coût en €.
// Ligne compacte (<summary>) + détail dépliable (<details>, accessible nativement).
// Toutes les valeurs sont insérées en texte (textContent), jamais en HTML.

const ROLE_LABELS = { planner: "découpage", worker: "partie", assembler: "assemblage" };   // multi-agents (10.5)
const NBSP = " ";                                    // espace fine insécable (typographie française)

export function fmtInt(n) {
  const s = String(Math.round(Math.abs(n || 0)));
  return (n < 0 ? "-" : "") + s.replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
}

export function fmtNum(v, decimals) {
  const [int, frac] = Math.abs(v).toFixed(decimals).split(".");
  return (v < 0 ? "-" : "") + fmtInt(+int) + (frac ? "," + frac : "");
}

/** 0 -> "0,00 €" ; micro-montants avec assez de décimales pour rester informatifs. */
export function fmtEur(v) {
  v = Number(v) || 0;
  if (v === 0) return `0,00${NBSP}€`;
  if (Math.abs(v) < 0.0001) return `<${NBSP}0,0001${NBSP}€`;
  const decimals = Math.abs(v) < 0.01 ? 4 : Math.abs(v) < 1 ? 3 : 2;
  let s = fmtNum(v, decimals);
  s = s.replace(/(,\d\d\d*?)0+$/, "$1");                  // retire les zéros au-delà de 2 décimales
  return `${s}${NBSP}€`;
}

export function fmtUsd(v) {
  v = Number(v) || 0;
  return v === 0 ? "0 $" : `${fmtNum(v, Math.abs(v) < 0.01 ? 6 : 4).replace(/0+$/, "").replace(/,$/, "")}${NBSP}$`;
}

export function fmtMs(ms) {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "–";
  return ms < 1000 ? `${Math.round(ms)}${NBSP}ms` : `${fmtNum(ms / 1000, ms < 10000 ? 1 : 0)}${NBSP}s`;
}

/** Résumé normalisé d'un méta `x_gateway` (+ usage, ttft) pour l'affichage. */
export function summarize(meta) {
  const u = meta.usage || {};
  const prompt = u.prompt_tokens || 0, completion = u.completion_tokens || 0;
  const cached = u.prompt_tokens_details?.cached_tokens || meta.cache?.cached_tokens || 0;
  const hit = !!meta.cache?.response_hit;
  const local = meta.hosting === "local";
  let costLabel = fmtEur(meta.cost_eur), costNote = "";
  if (hit) costNote = "cache";
  else if (local) costNote = "local";
  if (hit || local) costLabel = fmtEur(0);
  return {
    model: meta.model || "?", provider: meta.provider || "?", local,
    tokens: u.total_tokens || prompt + completion, prompt, completion, cached,
    latency: meta.latency_ms, ttft: meta.ttft_ms, costLabel, costNote, hit,
  };
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function row(dl, label, value) {
  if (value === null || value === undefined || value === "") return;
  dl.append(el("dt", "", label), el("dd", "", value));
}

export function renderMetrics(meta) {
  if (!meta || !meta.model) return null;
  const s = summarize(meta);
  const details = el("details", "metrics");
  const summary = el("summary");
  summary.setAttribute("aria-label",
    `Détails : ${s.model}, ${fmtInt(s.tokens)} tokens, ${fmtMs(s.latency)}, ${s.costLabel}`);

  const items = [
    [s.local ? "local" : "cloud", s.model, `${s.provider}/${s.model}`],
    ["tokens", `${fmtInt(s.tokens)} tokens`, `Entrée ${fmtInt(s.prompt)}${s.cached ? ` (dont ${fmtInt(s.cached)} en cache)` : ""} · sortie ${fmtInt(s.completion)}`],
    ["latency", fmtMs(s.latency), s.ttft !== undefined ? `Premier token après ${fmtMs(s.ttft)}` : "Latence totale"],
    ["cost", s.costNote ? `${s.costLabel} · ${s.costNote}` : s.costLabel, "Coût de cette réponse"],
  ];
  for (const [kind, text, title] of items) {
    const span = el("span", `m m-${kind}`, text);
    span.title = title;
    summary.append(span);
  }
  details.append(summary);

  const dl = el("dl", "metrics-detail");
  row(dl, "Modèle", `${s.provider}/${s.model} (${s.local ? "local" : "cloud"})`);
  row(dl, "Tokens", `${fmtInt(s.prompt)} en entrée · ${fmtInt(s.completion)} en sortie` +
    (meta.usage_estimated ? " (estimés : le fournisseur ne les a pas communiqués)" : ""));
  if (s.cached) row(dl, "Prompt caching", `${fmtInt(s.cached)} tokens lus en cache` +
    (meta.cache?.savings_usd ? ` · économie ${fmtUsd(meta.cache.savings_usd)}` : ""));
  row(dl, "Latence", `${fmtMs(s.latency)} au total` + (s.ttft !== undefined ? ` · premier token ${fmtMs(s.ttft)}` : ""));
  if (s.hit) row(dl, "Cache de réponses", `réponse resservie · ${fmtUsd(meta.cache.saved_usd)} économisés`);
  else if (s.local) row(dl, "Coût", `0,00${NBSP}€ (modèle local)`);
  else row(dl, "Coût", `${fmtEur(meta.cost_eur)} (${fmtUsd(meta.cost_usd)})`);
  if (meta.compression?.cost_usd) row(dl, "Dont résumé d'historique", fmtUsd(meta.compression.cost_usd));
  if (meta.routing) row(dl, "Routage", `${meta.routing.mode} · effort ${meta.effort || meta.routing.effort}` +
    (meta.routing.reason ? ` — ${meta.routing.reason}` : ""));
  else if (meta.effort) row(dl, "Effort", meta.effort);
  if (meta.fallback) row(dl, "Fallback", meta.fallback.attempts.map((a) => `${a.ref} (${a.status})`).join(", "));
  if (meta.orchestration) row(dl, "Multi-agents", meta.orchestration.agents
    .map((a) => `${a.ref} ${ROLE_LABELS[a.role] || a.role}${a.part ? ` ${a.part}` : ""}${a.ok === false ? " ✗" : ""}`)
    .join(" · "));
  if (meta.split?.parts > 1) row(dl, "Découpage", `message lu en ${meta.split.parts} parties` +
    ` (fenêtre de ${fmtInt(meta.split.window)} tokens) puis synthétisé` +
    (meta.split.parallel ? `, en parallèle sur ${meta.split.workers.length} serveurs` : "") +
    " — une demande qui exige une vue d'ensemble perd en qualité");
  if (meta.split?.trimmed_messages) row(dl, "Historique",
    `${meta.split.trimmed_messages} anciens messages non transmis (fenêtre de ${fmtInt(meta.split.window)} tokens)`);
  if (meta.compression) row(dl, "Historique", meta.compression.mode === "summary"
    ? `${meta.compression.summarized_messages} messages résumés` : `${meta.compression.trimmed_messages || 0} messages retirés`);
  details.append(dl);
  return details;
}
