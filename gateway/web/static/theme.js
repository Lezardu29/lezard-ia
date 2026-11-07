// Moteur de thèmes (5.3) — script classique chargé de façon synchrone dans <head> :
// il applique le thème mémorisé AVANT le premier rendu (pas de flash), puis expose
// window.GatewayTheme pour le panneau de personnalisation.
//
// Une palette complète (clair OU sombre) est générée à partir d'une couleur d'accent et d'une
// teinte de neutres ; les couleurs personnalisées (accent, fond, bulles, bordures) s'y superposent.
// Le texte et les surfaces sont dérivés du fond pour rester lisibles (contraste WCAG).
(function () {
  "use strict";
  var STORE = "gw.theme";

  var PRESETS = {
    // thème par défaut : couleurs du logo (vert « IA », neutres teintés du bleu marine de « Lezard »)
    "default":  { label: "Lezard IA",     accent: "#0b9476", hue: 212, sat: 22 },
    "indigo":   { label: "Indigo",        accent: "#4f46e5", hue: 240, sat: 6 },
    "carbon":   { label: "Dark Carbon",   accent: "#ea580c", hue: 0,   sat: 0 },
    "cyber":    { label: "Cyber Red",     accent: "#ff2e63", hue: 330, sat: 22 },
    "slate":    { label: "Minimal Slate", accent: "#475569", hue: 215, sat: 16 },
    "crimson":  { label: "Crimson",       accent: "#be123c", hue: 350, sat: 10 },
    "emerald":  { label: "Emerald",       accent: "#059669", hue: 160, sat: 12 },
    "midnight": { label: "Midnight Blue", accent: "#3b82f6", hue: 225, sat: 32 },
    // couleur de l'interface Claude : accent corail, neutres crème chauds ; couleurs fixes, accent ajusté pour le
    // contraste (4,9:1 en clair, 4,9:1 en sombre)
    // couleurs de texte fixées à 4,5:1 au moins sur leurs fonds (WCAG AA) : sourdine, accent sur fond doux
    "claude":   { label: "Claude",        accent: "#d97757", hue: 40,  sat: 18, fixed: {
      light: { "--bg": "#faf9f5", "--surface": "#ffffff", "--surface-2": "#f0eee6", "--border": "#e3dfd4",
               "--text": "#141413", "--text-muted": "#6c6a64", "--accent": "#a94d2c", "--accent-contrast": "#ffffff",
               "--accent-soft": "#f6e7df", "--bubble-user": "#f0eee6", "--code-bg": "#f5f4ee", "--code-head": "#ebe8de",
               "--sidebar": "#f3f1ea" },
      dark:  { "--bg": "#262624", "--surface": "#30302e", "--surface-2": "#3a3936", "--border": "#45443f",
               "--text": "#faf9f5", "--text-muted": "#a6a39b", "--accent": "#e09076", "--accent-contrast": "#141413",
               "--accent-soft": "#4a3329", "--bubble-user": "#141413", "--code-bg": "#1f1e1d", "--code-head": "#2b2a27",
               "--sidebar": "#1f1e1d" } } }
  };
  // par défaut : l'interface Claude et ses couleurs ; thème et couleurs se changent dans le panneau Apparence
  var DEFAULT_THEME = { mode: "auto", preset: "claude", ui: "claude" };
  // interface : « claude » (par défaut : mise en page de l'interface Claude, ui-claude.css) ou « classic » (Lezard IA) ;
  // choisie dans le panneau Apparence ou par l'adresse (?ui=claude, ?ui=classic)
  var UI_STORE = "gw.ui", UIS = ["classic", "claude"];

  // ---------- couleurs ----------
  function clamp(x, a, b) { return Math.min(b, Math.max(a, x)); }

  function normalize(value) {
    if (typeof value !== "string") return null;
    var v = value.trim().toLowerCase(), m;
    if ((m = v.match(/^#?([0-9a-f]{3})$/))) return "#" + m[1].split("").map(function (c) { return c + c; }).join("");
    if ((m = v.match(/^#?([0-9a-f]{6})$/))) return "#" + m[1];
    if ((m = v.match(/^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})/))) {
      var parts = [m[1], m[2], m[3]].map(Number);
      if (parts.every(function (n) { return n <= 255; }))
        return "#" + parts.map(function (n) { return ("0" + n.toString(16)).slice(-2); }).join("");
    }
    return null;
  }
  function toRgb(hex) { var n = parseInt(hex.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
  function toHex(rgb) { return "#" + rgb.map(function (c) { return ("0" + Math.round(clamp(c, 0, 255)).toString(16)).slice(-2); }).join(""); }

  function hsl(h, s, l) {
    s /= 100; l /= 100;
    var k = function (n) { return (n + h / 30) % 12; };
    var a = s * Math.min(l, 1 - l);
    var f = function (n) { return l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1))); };
    return toHex([f(0) * 255, f(8) * 255, f(4) * 255]);
  }
  function toHsl(hex) {
    var c = toRgb(hex).map(function (x) { return x / 255; });
    var max = Math.max.apply(null, c), min = Math.min.apply(null, c), l = (max + min) / 2, h = 0, s = 0;
    if (max !== min) {
      var d = max - min;
      s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
      h = max === c[0] ? (c[1] - c[2]) / d + (c[1] < c[2] ? 6 : 0) : max === c[1] ? (c[2] - c[0]) / d + 2 : (c[0] - c[1]) / d + 4;
      h *= 60;
    }
    return [h, s * 100, l * 100];
  }
  function luminance(hex) {
    var c = toRgb(hex).map(function (x) { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }
  function contrast(a, b) {
    var la = luminance(a), lb = luminance(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }
  function mix(a, b, t) {
    var x = toRgb(a), y = toRgb(b);
    return toHex([0, 1, 2].map(function (i) { return x[i] * t + y[i] * (1 - t); }));
  }
  // Ajuste la luminosité d'une couleur jusqu'à atteindre le contraste voulu sur `bg`.
  function ensureContrast(color, bg, min) {
    if (contrast(color, bg) >= min) return color;
    var p = toHsl(color), darker = luminance(bg) > 0.4;
    for (var i = 0; i < 60; i++) {
      p[2] = clamp(p[2] + (darker ? -2 : 2), 0, 100);
      var c = hsl(p[0], p[1], p[2]);
      if (contrast(c, bg) >= min) return c;
    }
    return darker ? "#000000" : "#ffffff";
  }
  function onColor(bg) { return contrast("#ffffff", bg) >= contrast("#111114", bg) ? "#ffffff" : "#111114"; }

  // ---------- palettes ----------
  function systemDark() {
    try { return window.matchMedia("(prefers-color-scheme: dark)").matches; } catch (e) { return false; }
  }
  function resolveScheme(theme) {
    var bg = theme.custom && normalize(theme.custom.background);
    if (bg) return luminance(bg) < 0.18 ? "dark" : "light";           // un fond perso impose le schéma
    if (theme.mode === "dark" || theme.mode === "light") return theme.mode;
    return systemDark() ? "dark" : "light";
  }

  function neutrals(scheme, h, s, base) {
    if (base) {                                      // dérivées d'un fond personnalisé
      var p = toHsl(base), dir = scheme === "dark" ? 1 : -1;
      var at = function (dl) { return hsl(p[0], p[1], clamp(p[2] + dir * dl, 0, 100)); };
      return { bg: base, surface: scheme === "dark" ? at(3.5) : (p[2] > 96 ? "#ffffff" : at(-2.5)),
               surface2: at(7), border: at(13), codeBg: at(-1.5 * dir), codeHead: at(5) };
    }
    return scheme === "dark"
      ? { bg: hsl(h, s, 6), surface: hsl(h, s, 9.5), surface2: hsl(h, s, 13), border: hsl(h, s, 19),
          codeBg: hsl(h, s, 5), codeHead: hsl(h, s, 11) }
      : { bg: hsl(h, s, 97.5), surface: "#ffffff", surface2: hsl(h, s, 94.5), border: hsl(h, s, 88),
          codeBg: hsl(h, s, 96.5), codeHead: hsl(h, s, 92) };
  }

  function palette(theme) {
    theme = theme || DEFAULT_THEME;
    var preset = PRESETS[theme.preset] || PRESETS["default"];
    var custom = theme.custom || {};
    var scheme = resolveScheme(theme);
    var n = neutrals(scheme, preset.hue, preset.sat, normalize(custom.background));
    var textBase = scheme === "dark" ? hsl(preset.hue, preset.sat * 0.3, 93) : hsl(preset.hue, preset.sat * 0.5, 10);
    var text = ensureContrast(textBase, n.bg, 12);
    var muted = ensureContrast(scheme === "dark" ? hsl(preset.hue, preset.sat * 0.5, 63) : hsl(preset.hue, preset.sat * 0.5, 42), n.bg, 4.5);
    var userAccent = normalize(custom.accent);
    // l'accent d'un préréglage est ajusté pour rester lisible ; un accent choisi est respecté tel quel
    var accent = userAccent || ensureContrast(preset.accent, n.surface, 4.5);
    var fixed = preset.fixed && !Object.keys(custom).length ? preset.fixed[scheme] : {};
    return {
      scheme: scheme,
      vars: Object.assign({
        "--bg": n.bg, "--surface": n.surface, "--surface-2": n.surface2,
        "--border": normalize(custom.border) || n.border,
        "--text": text, "--text-muted": muted,
        "--accent": accent, "--accent-contrast": onColor(accent),
        "--accent-soft": mix(accent, n.surface, scheme === "dark" ? 0.18 : 0.1),
        "--bubble-user": normalize(custom.bubble) || n.surface2,
        "--code-bg": n.codeBg, "--code-head": n.codeHead, "--sidebar": n.surface
      }, fixed)
    };
  }

  function report(theme) {
    var v = palette(theme).vars;
    var r = function (a, b) { return Math.round(contrast(a, b) * 10) / 10; };
    return { text: r(v["--text"], v["--bg"]), accent: r(v["--accent"], v["--bg"]),
             onAccent: r(v["--accent-contrast"], v["--accent"]),
             bubble: r(v["--text"], v["--bubble-user"]) };
  }

  // ---------- application & mémorisation ----------
  var listeners = [];
  var current = null;

  // l'interface fait partie du thème : enregistrée sur le compte, elle suit sur tous les appareils
  function getUi() {
    return current && UIS.indexOf(current.ui) >= 0 ? current.ui : "claude";
  }
  function apply(theme) {
    current = theme || DEFAULT_THEME;
    var ui = getUi(), root = document.documentElement;
    // interface Claude : les couleurs de Claude, sauf thème ou couleurs choisis (un ancien thème « Lezard IA » jamais
    // changé, sans interface choisie, prend celles de Claude)
    var legacy = ui === "claude" && !current.ui && (current.preset || "default") === "default"
      && !(current.custom && Object.keys(current.custom).length);
    var p = palette(legacy ? { mode: current.mode, preset: "claude" } : current);
    root.setAttribute("data-ui", ui);
    Object.keys(p.vars).forEach(function (k) { root.style.setProperty(k, p.vars[k]); });
    root.setAttribute("data-theme", p.scheme);         // pilote aussi la coloration du code
    applyPack(current.pack);
    root.style.colorScheme = p.scheme;
    listeners.forEach(function (fn) { try { fn(current, p); } catch (e) { /* ignoré */ } });
  }
  // interface personnalisée importée (.zip du panneau Apparence, ui_packs.py) : sa feuille passe après toutes celles
  // de Lezard IA (theme.js est chargé après elles) ; <html data-pack> la signale aux autres feuilles
  var PACK_ID = /^pk_[0-9a-f]{24}$/;
  function applyPack(pack) {
    var root = document.documentElement, link = document.getElementById("ui-pack-css");
    if (!pack || !PACK_ID.test(pack)) {
      if (link) link.remove();
      root.removeAttribute("data-pack");
      return;
    }
    var href = "/ui-packs/" + pack + "/style.css";
    if (!link) {
      link = document.createElement("link");
      link.id = "ui-pack-css";
      link.rel = "stylesheet";
    }
    if (link.getAttribute("href") !== href) link.setAttribute("href", href);
    (document.head || root).appendChild(link);          // toujours la dernière feuille
    root.setAttribute("data-pack", pack);
  }

  function load() {
    try { var t = JSON.parse(localStorage.getItem(STORE)); return t && typeof t === "object" ? t : null; }
    catch (e) { return null; }
  }
  function save(theme) {
    try { localStorage.setItem(STORE, JSON.stringify(theme)); } catch (e) { /* privé / plein */ }
  }
  function set(theme, opts) {
    var t = { mode: theme.mode || "auto", preset: theme.preset || "default" };
    if (theme.custom && Object.keys(theme.custom).length) t.custom = theme.custom;
    if (UIS.indexOf(theme.ui) >= 0) t.ui = theme.ui;             // interface suivie avec le thème (compte)
    if (theme.pack && PACK_ID.test(theme.pack)) t.pack = theme.pack;   // interface personnalisée importée
    t.updated_at = (opts && opts.updatedAt) || Date.now() / 1000;
    save(t);
    apply(t);
    return t;
  }

  function setUi(ui) {
    if (UIS.indexOf(ui) < 0) return null;
    var cur = current || DEFAULT_THEME, preset = cur.preset || "default";
    var plain = !(cur.custom && Object.keys(cur.custom).length);
    // couleurs de chaque interface quand on n'en a pas choisi d'autres : Claude ↔ Lezard IA
    if (plain && ui === "claude" && preset === "default") preset = "claude";
    if (plain && ui === "classic" && preset === "claude") preset = "default";
    return set({ mode: cur.mode, preset: preset, custom: cur.custom, ui: ui, pack: cur.pack });
  }

  // ?ui= dans l'adresse (de la page ou du cadre principal), ou ancien choix gardé sur cet appareil : repris dans le thème
  var startTheme = load() || DEFAULT_THEME, askedUi = null;
  try { askedUi = new URLSearchParams((window.top || window).location.search).get("ui"); } catch (e) { /* autre origine */ }
  try {
    if (UIS.indexOf(askedUi) < 0) askedUi = localStorage.getItem(UI_STORE);
    localStorage.removeItem(UI_STORE);
  } catch (e) { /* privé */ }
  if (UIS.indexOf(askedUi) >= 0 && askedUi !== (startTheme.ui || "claude")) {
    current = startTheme;
    setUi(askedUi);                                     // plus récent : envoyé au compte par le panneau
  } else {
    apply(startTheme);
  }
  // ?pack=aucun : retire l'interface personnalisée (porte de sortie si elle rend la page inutilisable) ; plus récent
  // que le choix du compte, il y est envoyé par le panneau Apparence
  var askedPack = null;
  try { askedPack = new URLSearchParams((window.top || window).location.search).get("pack"); } catch (e) { /* autre origine */ }
  if (askedPack === "aucun" && current && current.pack) {
    set({ mode: current.mode, preset: current.preset, custom: current.custom, ui: current.ui });
  }
  // les autres pages du cadre principal (et les autres onglets) suivent le changement d'interface ou de thème
  window.addEventListener("storage", function (e) {
    if (e.key === STORE) apply(load() || DEFAULT_THEME);
  });
  try {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
      if (current && current.mode === "auto") apply(current);
    });
  } catch (e) { /* vieux navigateurs */ }

  window.GatewayTheme = {
    PRESETS: PRESETS, DEFAULT: DEFAULT_THEME,
    get: function () { return current; }, stored: load, set: set, apply: apply,
    palette: palette, report: report, ui: getUi, setUi: setUi, UIS: UIS, normalize: normalize, resolveScheme: resolveScheme,
    onChange: function (fn) { listeners.push(fn); }
  };
})();
