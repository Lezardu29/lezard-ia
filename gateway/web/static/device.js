// Détection des capacités de l'appareil (13.5), chargée dans <head> pour éviter tout saut d'affichage.
// Par les capacités de l'écran (matchMedia), pas par le User-Agent : falsifiable, et faux pour une tablette
// avec clavier ou un PC tactile. Le User-Agent ne sert que d'indice quand le navigateur ignore « pointer ».
//   <html data-input="touch|mouse">  : pointeur principal grossier (doigt) ou fin (souris, stylet)
//   <html data-layout="compact|wide"> : écran étroit (≤ 640 px), même règle que les @media des feuilles de style
(function () {
  var root = document.documentElement;
  var mq = function (q) { return window.matchMedia ? window.matchMedia(q) : null; };
  var coarse = mq("(pointer: coarse)"), known = mq("(pointer: fine), (pointer: coarse), (pointer: none)");
  var narrow = mq("(max-width: 640px)");
  var uaHint = /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent || "");
  function apply() {
    var touch = known && known.matches ? coarse.matches : uaHint;      // indice UA seulement en dernier recours
    root.setAttribute("data-input", touch ? "touch" : "mouse");
    root.setAttribute("data-layout", narrow && narrow.matches ? "compact" : "wide");
  }
  apply();
  [coarse, narrow].forEach(function (m) {
    if (!m) return;
    if (m.addEventListener) m.addEventListener("change", apply); else if (m.addListener) m.addListener(apply);
  });
})();
