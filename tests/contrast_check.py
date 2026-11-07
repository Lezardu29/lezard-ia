"""Mesure du contraste des textes (WCAG AA), partagée par les tests dans le navigateur."""

# Contraste réel (WCAG AA) des textes visibles : couleur calculée par le navigateur sur le fond effectif ;
# 4,5:1 (3:1 pour les grands textes). Renvoie les textes en dessous.
CONTRAST = r"""() => {
  const lum = (c) => { const [r, g, b] = c.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
  const parse = (s) => { const m = s.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(/[ ,/]+/).map(Number); return { c: p.slice(0, 3), a: p[3] ?? 1 }; };
  const bgOf = (el) => { let stack = []; for (let e = el; e; e = e.parentElement) { const b = parse(getComputedStyle(e).backgroundColor); if (b && b.a > 0) { stack.push(b); if (b.a >= 1) break; } }
    let c = [255, 255, 255]; if (stack.length && stack[stack.length - 1].a >= 1) c = stack.pop().c; else c = document.documentElement.matches("[data-theme=dark], .dark") ? [0, 0, 0] : c;
    for (const b of stack.reverse()) c = c.map((v, i) => b.c[i] * b.a + v * (1 - b.a)); return c; };
  const out = new Map();
  for (const el of document.querySelectorAll("body *")) {
    if (!el.childNodes.length || ![...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim())) continue;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (!r.width || !r.height || s.visibility === "hidden" || el.closest("[hidden],[aria-hidden=true]") || parseFloat(s.opacity) < .5) continue;
    if (el.closest("button:disabled, input:disabled, .disabled")) continue;
    const fg = parse(s.color); if (!fg) continue;
    const bg = bgOf(el), f = fg.c.map((v, i) => v * fg.a + bg[i] * (1 - fg.a));
    const L1 = lum(f), L2 = lum(bg), ratio = (Math.max(L1, L2) + .05) / (Math.min(L1, L2) + .05);
    const big = parseFloat(s.fontSize) >= 24 || (parseFloat(s.fontSize) >= 18.66 && parseInt(s.fontWeight) >= 700);
    if (ratio < (big ? 3 : 4.5)) { const key = `${ratio.toFixed(2)} ${s.color} sur rgb(${bg.map(Math.round)}) : ${el.tagName.toLowerCase()}.${[...el.classList].join(".")}`; if (!out.has(key)) out.set(key, el.textContent.trim().slice(0, 40)); }
  }
  return [...out].slice(0, 15).map(([k, v]) => `${k} « ${v} »`);
}"""
