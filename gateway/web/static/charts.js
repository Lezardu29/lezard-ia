// Graphiques interactifs minimalistes du tableau de bord (6.4), en SVG pur, sans dépendance.
// Méthode : skill dataviz — traits fins, grille en filet discrète, couleur fixe par entité,
// infobulle à chaque graphique, vue tableau systématique (les infobulles ne sont jamais le seul accès).
import { fmtEur, fmtInt, fmtMs } from "./metrics.js";

const NS = "http://www.w3.org/2000/svg";
// Couleur fixe par entité, dans l'ordre validé (slots 1→5) ; tout le reste = « Autres » neutre.
// « Cloud gratuit » (offres gratuites officielles, 13.8) prend le 5e slot ; Groq payant rejoint « Autres ».
const ENTITIES = [
  ["local", "Local", "var(--series-1)"], ["openai", "OpenAI", "var(--series-2)"],
  ["anthropic", "Anthropic", "var(--series-3)"], ["mistral", "Mistral", "var(--series-4)"],
  ["free", "Cloud gratuit", "var(--series-5)"], ["other", "Autres", "var(--series-other)"],
];
const ENTITY = Object.fromEntries(ENTITIES.map(([k, label, color]) => [k, { label, color }]));
const SCATTER_COLORED = new Set(["local", "openai", "anthropic"]);   // 3 slots validés « toutes paires »
const entityOf = (provider, hosting) =>
  hosting === "local" || provider === "local" ? "local"
    : hosting === "free" || provider === "free" ? "free" : ENTITY[provider] ? provider : "other";

// ---------- utilitaires ----------
function svg(tag, attrs = {}, text) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  return e;
}
function html(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
function niceStep(x) {
  const exp = Math.floor(Math.log10(x)), f = x / 10 ** exp;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * 10 ** exp;
}
export function niceScale(max, count = 4) {
  if (!(max > 0)) return { max: 1, ticks: [0, 0.25, 0.5, 0.75, 1] };
  const step = niceStep(max / count);
  const top = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = 0; v <= top + step / 2; v += step) ticks.push(+v.toPrecision(12));
  return { max: top, ticks };
}
const fmtTime = (t, bucket) => {
  const d = new Date(t * 1000);
  return bucket === "hour" ? `${d.getHours()} h` : d.toLocaleDateString("fr-FR", { day: "numeric", month: "short" });
};
const fmtFull = (t, bucket) => {
  const d = new Date(t * 1000);
  return bucket === "hour"
    ? d.toLocaleString("fr-FR", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long" });
};

// ---------- infobulle partagée (valeur d'abord, nom ensuite) ----------
let tip;
function showTip(x, y, title, rows) {
  if (!tip) {
    tip = html("div", "chart-tip");
    tip.setAttribute("aria-hidden", "true");            // les tableaux portent les mêmes valeurs
    document.body.append(tip);
  }
  tip.replaceChildren(html("div", "tip-title", title));
  for (const [color, value, label] of rows) {
    const row = html("div", "tip-row");
    const key = html("span", "tip-key");
    key.style.background = color;
    row.append(key, html("strong", "", value), html("span", "", label));
    tip.append(row);
  }
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  tip.style.left = `${Math.min(window.innerWidth - r.width - 8, x + 14)}px`;
  tip.style.top = `${Math.max(8, y - r.height - 10)}px`;
}
const hideTip = () => { if (tip) tip.hidden = true; };

function legend(items) {
  const ul = html("ul", "legend");
  for (const [color, label, value, shape] of items) {
    const li = html("li");
    const key = html("span", `key${shape === "line" ? " line" : ""}`);
    key.style.background = color;
    li.append(key, html("span", "", label));
    if (value) li.append(html("span", "val", value));
    ul.append(li);
  }
  return ul;
}
function table(headers, rows) {
  const t = html("table");
  const thead = html("thead"), tr = html("tr");
  headers.forEach(([h, num]) => { const th = html("th", num ? "num" : "", h); th.scope = "col"; tr.append(th); });
  thead.append(tr);
  const tbody = html("tbody");
  for (const r of rows) {
    const row = html("tr");
    r.forEach((c, i) => row.append(html("td", headers[i][1] ? "num" : "", c)));
    tbody.append(row);
  }
  t.append(thead, tbody);
  return t;
}
const empty = (msg) => html("p", "chart-empty", msg);

// ---------- 1. évolution des coûts (€) ----------
function renderCost(card, data) {
  const { points, bucket, baseline_model: base } = data;
  const body = card.querySelector(".chart-body"), W = Math.max(300, body.clientWidth), H = 230;
  const m = { l: 62, r: 96, t: 12, b: 26 }, iw = W - m.l - m.r, ih = H - m.t - m.b;
  const hasRef = base && points.some((p) => p.baseline_eur > 0);
  const maxV = Math.max(...points.map((p) => Math.max(p.cost_eur, hasRef ? p.baseline_eur : 0)), 0);
  if (!points.some((p) => p.requests)) { body.replaceChildren(empty("Aucune requête sur la période.")); return; }
  const sc = niceScale(maxV);
  const x = (i) => m.l + (points.length === 1 ? iw / 2 : (i / (points.length - 1)) * iw);
  const y = (v) => m.t + ih - (v / sc.max) * ih;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
                         "aria-label": `Évolution des coûts en euros, ${points.length} points ; détail dans la vue tableau` });
  for (const t of sc.ticks) {                              // grille horizontale en filet
    s.append(svg("line", { class: t ? "grid-line" : "axis-line", x1: m.l, x2: m.l + iw, y1: y(t), y2: y(t) }));
    s.append(svg("text", { class: "tick", x: m.l - 8, y: y(t) + 4, "text-anchor": "end" }, fmtEur(t)));
  }
  const every = Math.max(1, Math.ceil(points.length / Math.max(2, Math.floor(iw / 70))));
  points.forEach((p, i) => {
    if (i % every === 0) s.append(svg("text", { class: "tick", x: x(i), y: H - 6, "text-anchor": "middle" }, fmtTime(p.t, bucket)));
  });
  const line = (key) => points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join("");
  const area = `${line("cost_eur")}L${x(points.length - 1)},${y(0)}L${x(0)},${y(0)}Z`;
  s.append(svg("path", { d: area, fill: "var(--accent)", "fill-opacity": "0.1" }));
  if (hasRef) s.append(svg("path", { d: line("baseline_eur"), class: "series-line", stroke: "var(--text-muted)" }));
  s.append(svg("path", { d: line("cost_eur"), class: "series-line", stroke: "var(--accent)" }));

  // étiquettes en bout de ligne (sauf si elles se chevauchent : la légende suffit alors)
  const last = points[points.length - 1], yc = y(last.cost_eur), yr = hasRef ? y(last.baseline_eur) : null;
  if (!hasRef || Math.abs(yc - yr) >= 14) {
    s.append(svg("text", { class: "end-label", x: x(points.length - 1) + 8, y: yc + 4 }, `Réel ${fmtEur(last.cost_eur)}`));
    if (hasRef) s.append(svg("text", { class: "end-label", x: x(points.length - 1) + 8, y: yr + 4 }, `Réf. ${fmtEur(last.baseline_eur)}`));
  }

  // réticule : la ligne verticale trouve le point le plus proche ; clavier ← →
  const cross = svg("line", { class: "crosshair", y1: m.t, y2: m.t + ih, visibility: "hidden" });
  const dotC = svg("circle", { r: 4, class: "dot", fill: "var(--accent)", visibility: "hidden" });
  const dotR = svg("circle", { r: 4, class: "dot", fill: "var(--text-muted)", visibility: "hidden" });
  const hit = svg("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent", tabindex: 0,
                            "aria-label": "Parcourir les points avec les flèches gauche et droite" });
  s.append(cross, dotR, dotC, hit);
  let idx = points.length - 1;
  const focusPoint = (i, cx, cy) => {
    idx = Math.max(0, Math.min(points.length - 1, i));
    const p = points[idx], px = x(idx);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("visibility", "visible");
    dotC.setAttribute("cx", px); dotC.setAttribute("cy", y(p.cost_eur)); dotC.setAttribute("visibility", "visible");
    if (hasRef) { dotR.setAttribute("cx", px); dotR.setAttribute("cy", y(p.baseline_eur)); dotR.setAttribute("visibility", "visible"); }
    const rows = [["var(--accent)", fmtEur(p.cost_eur), "coût réel"]];
    if (hasRef) rows.push(["var(--text-muted)", fmtEur(p.baseline_eur), `tout sur ${base}`]);
    rows.push(["transparent", fmtInt(p.requests), "requêtes"]);
    const box = s.getBoundingClientRect();
    showTip(cx ?? box.left + (px / W) * box.width, cy ?? box.top + (y(p.cost_eur) / H) * box.height, fmtFull(p.t, bucket), rows);
  };
  const clear = () => { [cross, dotC, dotR].forEach((e) => e.setAttribute("visibility", "hidden")); hideTip(); };
  hit.addEventListener("pointermove", (e) => {
    const box = s.getBoundingClientRect(), px = ((e.clientX - box.left) / box.width) * W;
    focusPoint(Math.round(((px - m.l) / iw) * (points.length - 1)), e.clientX, e.clientY);
  });
  hit.addEventListener("pointerleave", clear);
  hit.addEventListener("blur", clear);
  hit.addEventListener("focus", () => focusPoint(idx));
  hit.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); focusPoint(idx + (e.key === "ArrowRight" ? 1 : -1)); }
  });
  body.replaceChildren(s);
  const items = [["var(--accent)", "Coût réel", null, "line"]];
  if (hasRef) items.push(["var(--text-muted)", `Référence : tout sur ${base}`, null, "line"]);
  body.append(legend(items));
  card.querySelector(".chart-table").replaceChildren(table(
    [["Période"], ["Requêtes", true], ["Coût réel", true], ["Référence", true]],
    points.filter((p) => p.requests).map((p) => [fmtFull(p.t, bucket), fmtInt(p.requests), fmtEur(p.cost_eur),
                                                   hasRef ? fmtEur(p.baseline_eur) : "–"])));
}

// ---------- 2. répartition local / cloud (donut ≤ 6 parts) ----------
function renderSplit(card, split) {
  const body = card.querySelector(".chart-body");
  const agg = Object.fromEntries(ENTITIES.map(([k]) => [k, { requests: 0, tokens: 0, cost: 0 }]));
  for (const s of split) {
    const a = agg[entityOf(s.provider, s.provider === "local" ? "local" : "cloud")];
    a.requests += s.requests; a.tokens += s.tokens; a.cost += s.cost_eur;
  }
  const total = Object.values(agg).reduce((t, a) => t + a.requests, 0);
  if (!total) { body.replaceChildren(empty("Aucune requête sur la période.")); return; }
  const segs = ENTITIES.filter(([k]) => agg[k].requests > 0);         // ordre FIXE, jamais trié par taille
  const S = 220, R = 92, r = 62, c = S / 2;
  const s = svg("svg", { viewBox: `0 0 ${S} ${S}`, role: "img",
                         "aria-label": `Répartition des requêtes : ${segs.map(([k, l]) => `${l} ${Math.round(agg[k].requests / total * 100)} %`).join(", ")}` });
  s.style.maxWidth = "240px";                              // CSSOM : autorisé par la CSP (pas d'attribut style)
  s.style.margin = "0 auto";
  let a0 = -Math.PI / 2;
  const nodes = [];
  for (const [k, label, color] of segs) {
    const frac = agg[k].requests / total, a1 = a0 + frac * 2 * Math.PI;
    const big = a1 - a0 > Math.PI ? 1 : 0, p = (rad, ang) => `${c + rad * Math.cos(ang)},${c + rad * Math.sin(ang)}`;
    const d = frac >= 0.9999
      ? `M${c},${c - R}A${R},${R} 0 1 1 ${c - 0.01},${c - R}L${c - 0.01},${c - r}A${r},${r} 0 1 0 ${c},${c - r}Z`
      : `M${p(R, a0)}A${R},${R} 0 ${big} 1 ${p(R, a1)}L${p(r, a1)}A${r},${r} 0 ${big} 0 ${p(r, a0)}Z`;
    const path = svg("path", { d, fill: color, class: "donut-seg" });
    path.addEventListener("pointermove", (e) => {
      nodes.forEach((n) => n.classList.toggle("dim", n !== path));
      showTip(e.clientX, e.clientY, label, [[color, `${(frac * 100).toFixed(1).replace(".", ",")} %`, `${fmtInt(agg[k].requests)} requêtes`],
        ["transparent", fmtInt(agg[k].tokens), "tokens"], ["transparent", fmtEur(agg[k].cost), "coût réel"]]);
    });
    path.addEventListener("pointerleave", () => { nodes.forEach((n) => n.classList.remove("dim")); hideTip(); });
    nodes.push(path);
    s.append(path);
    a0 = a1;
  }
  const localPct = Math.round((agg.local.requests / total) * 100);
  s.append(svg("text", { class: "donut-hero", x: c, y: c + 4, "text-anchor": "middle" }, `${localPct} %`));
  s.append(svg("text", { class: "donut-sub", x: c, y: c + 22, "text-anchor": "middle" }, "en local"));
  body.replaceChildren(s, legend(segs.map(([k, label, color]) =>
    [color, label, `${Math.round((agg[k].requests / total) * 100)} %`, "rect"])));
  card.querySelector(".chart-table").replaceChildren(table(
    [["Hébergement"], ["Requêtes", true], ["Part", true], ["Tokens", true], ["Coût réel", true]],
    segs.map(([k, label]) => [label, fmtInt(agg[k].requests), `${((agg[k].requests / total) * 100).toFixed(1).replace(".", ",")} %`,
                              fmtInt(agg[k].tokens), fmtEur(agg[k].cost)])));
}

// ---------- 3. coût vs latence (nuage de points) ----------
function renderScatter(card, pts) {
  const body = card.querySelector(".chart-body");
  if (!pts.length) { body.replaceChildren(empty("Aucune requête sur la période.")); return; }
  const W = Math.max(300, body.clientWidth), H = 230, m = { l: 62, r: 14, t: 12, b: 30 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const sx = niceScale(Math.max(...pts.map((p) => p.latency_ms)), 5), sy = niceScale(Math.max(...pts.map((p) => p.cost_eur)));
  const x = (v) => m.l + (v / sx.max) * iw, y = (v) => m.t + ih - (v / sy.max) * ih;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
                         "aria-label": `Coût selon la latence, ${pts.length} requêtes ; détail dans la vue tableau` });
  for (const t of sy.ticks) {
    s.append(svg("line", { class: t ? "grid-line" : "axis-line", x1: m.l, x2: m.l + iw, y1: y(t), y2: y(t) }));
    s.append(svg("text", { class: "tick", x: m.l - 8, y: y(t) + 4, "text-anchor": "end" }, fmtEur(t)));
  }
  for (const t of sx.ticks) s.append(svg("text", { class: "tick", x: x(t), y: H - 12, "text-anchor": "middle" }, fmtMs(t)));
  s.append(svg("text", { class: "tick", x: m.l + iw, y: H - 1, "text-anchor": "end" }, "latence →"));
  const ent = (p) => { const e = entityOf(p.provider, p.hosting); return SCATTER_COLORED.has(e) ? e : "other"; };
  const placed = pts.map((p) => ({ p, e: ent(p), cx: x(p.latency_ms), cy: y(p.cost_eur) }));
  // « Autres » dessinés dessous, entités colorées par-dessus
  for (const d of [...placed.filter((d) => d.e === "other"), ...placed.filter((d) => d.e !== "other")]) {
    d.node = svg("circle", { cx: d.cx, cy: d.cy, r: 4, class: "dot", fill: ENTITY[d.e].color });
    s.append(d.node);
  }
  // cible de survol : le point le plus proche (≤ 24 px), pas le pixel exact du point
  const hit = svg("rect", { x: 0, y: 0, width: W, height: H, fill: "transparent" });
  let current = null;
  hit.addEventListener("pointermove", (e) => {
    const box = s.getBoundingClientRect(), k = W / box.width;
    const px = (e.clientX - box.left) * k, py = (e.clientY - box.top) * k;
    let best = null, bd = (24 * k) ** 2;
    for (const d of placed) { const dd = (d.cx - px) ** 2 + (d.cy - py) ** 2; if (dd < bd) { bd = dd; best = d; } }
    if (current && current !== best) current.node.classList.remove("hover");
    current = best;
    if (!best) return hideTip();
    best.node.classList.add("hover");
    showTip(e.clientX, e.clientY, `${best.p.provider}/${best.p.model}`, [
      [ENTITY[best.e].color, fmtEur(best.p.cost_eur), "coût"], ["transparent", fmtMs(best.p.latency_ms), "latence"],
      ["transparent", new Date(best.p.ts * 1000).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }), ""]]);
  });
  hit.addEventListener("pointerleave", () => { current?.node.classList.remove("hover"); hideTip(); });
  s.append(hit);
  const present = ENTITIES.filter(([k]) => placed.some((d) => d.e === k));
  body.replaceChildren(s, legend(present.map(([k, label, color]) =>
    [color, k === "other" ? "Autres fournisseurs" : label, `${placed.filter((d) => d.e === k).length}`, "rect"])));
  card.querySelector(".chart-table").replaceChildren(table(
    [["Date"], ["Modèle"], ["Latence", true], ["Coût", true]],
    pts.slice(0, 200).map((p) => [new Date(p.ts * 1000).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }),
                                  `${p.provider}/${p.model}`, fmtMs(p.latency_ms), fmtEur(p.cost_eur)])));
}

// ---------- 4. carte de chaleur de l'activité (jour × heure) ----------
// une seule teinte (l'accent), clair → foncé ; le 1er palier reste distinct d'une case vide en sombre
// 10 paliers : les écarts d'activité se lisent plus finement
const HEAT_STEPS = [24, 32, 40, 48, 56, 64, 72, 81, 90, 100];
const heatColor = (n, max) => n === 0 ? "var(--surface-2)"
  : `color-mix(in srgb, var(--accent) ${HEAT_STEPS[Math.min(HEAT_STEPS.length - 1,
      Math.floor((n / max) * HEAT_STEPS.length - 1e-9))]}%, var(--surface))`;

function renderHeat(card, heat) {
  const body = card.querySelector(".chart-body");
  if (!heat.max) { body.replaceChildren(empty("Aucune activité sur la période.")); return; }
  const W = Math.max(300, body.clientWidth), left = 38, top = 16, gap = 2;
  const cw = (W - left) / 24, ch = Math.min(22, Math.max(14, cw * 0.9)), H = top + 7 * ch + 4;
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
                         "aria-label": "Activité par jour de la semaine et par heure ; détail dans la vue tableau" });
  for (let h = 0; h < 24; h += 3) s.append(svg("text", { class: "tick", x: left + h * cw + cw / 2, y: 11, "text-anchor": "middle" }, `${h} h`));
  heat.counts.forEach((row, d) => {
    s.append(svg("text", { class: "tick", x: left - 6, y: top + d * ch + ch / 2 + 4, "text-anchor": "end" }, heat.days[d]));
    row.forEach((n, h) => {
      const cell = svg("rect", { x: left + h * cw + gap / 2, y: top + d * ch + gap / 2, width: cw - gap, height: ch - gap, class: "heat-cell" });
      cell.style.fill = heatColor(n, heat.max);
      cell.addEventListener("pointermove", (e) => {
        cell.classList.add("hover");
        showTip(e.clientX, e.clientY, `${heat.days[d]} ${h} h – ${h + 1} h`, [["transparent", fmtInt(n), n > 1 ? "requêtes" : "requête"]]);
      });
      cell.addEventListener("pointerleave", () => { cell.classList.remove("hover"); hideTip(); });
      s.append(cell);
    });
  });
  const scale = html("div", "heat-scale");
  scale.append(html("span", "", "Moins"));
  for (const p of [0, ...HEAT_STEPS]) {
    const sw = html("span", "sw");
    sw.style.background = p ? `color-mix(in srgb, var(--accent) ${p}%, var(--surface))` : "var(--surface-2)";
    scale.append(sw);
  }
  scale.append(html("span", "", `Plus (max ${fmtInt(heat.max)} requêtes/h)`));
  body.replaceChildren(s, scale);
  const rows = [];
  heat.counts.forEach((row, d) => row.forEach((n, h) => { if (n) rows.push([d, h, n]); }));
  rows.sort((a, b) => b[2] - a[2]);
  card.querySelector(".chart-table").replaceChildren(table([["Jour"], ["Heure"], ["Requêtes", true]],
    rows.map(([d, h, n]) => [heat.days[d], `${h} h – ${h + 1} h`, fmtInt(n)])));
}

// ---------- orchestration ----------
const RENDER = { cost: (c, d) => renderCost(c, d.timeseries), split: (c, d) => renderSplit(c, d.split),
                 scatter: (c, d) => renderScatter(c, d.scatter), heat: (c, d) => renderHeat(c, d.heatmap) };
let lastData = null;

export function renderCharts(data) {
  lastData = data;
  hideTip();                                               // une infobulle ne survit pas à son graphique
  document.querySelectorAll(".chart-card[data-chart]").forEach((card) => {
    card.classList.remove("loading");
    RENDER[card.dataset.chart](card, data);
  });
}

export function setChartsLoading() {
  document.querySelectorAll(".chart-card").forEach((c) => c.classList.add("loading"));   // garde le cadre
}

export function initCharts() {
  document.querySelectorAll(".chart-card .view-toggle").forEach((b) => b.addEventListener("click", () => {
    const card = b.closest(".chart-card"), on = card.classList.toggle("show-table");
    b.setAttribute("aria-pressed", String(on));
    b.textContent = on ? "Graphique" : "Tableau";
  }));
  const grid = document.querySelector(".charts");
  // largeur de départ connue : le 1er rappel de l'observateur ne déclenche pas un 2e rendu inutile
  let raf = 0, lastW = Math.round(grid.getBoundingClientRect().width);
  new ResizeObserver((entries) => {                          // redessine à la nouvelle largeur
    const w = Math.round(entries[0].contentRect.width);
    if (!lastData || w === lastW) return;
    lastW = w;
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => renderCharts(lastData));
  }).observe(grid);
  window.addEventListener("scroll", hideTip, { passive: true });
}
