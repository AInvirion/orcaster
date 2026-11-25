// theme-boot.js — applies the persisted theme before the first paint (avoids FOUC).
// It goes in <head> as a synchronous external script: the /admin CSP does not
// allow inline scripts. It reads the preference; if there is none, the CSS
// prefers-color-scheme decides.
(function () {
  try {
    var t = localStorage.getItem('to-theme');
    if (t === 'dark' || t === 'light') {
      document.documentElement.setAttribute('data-theme', t);
    }
  } catch (_) { /* storage blocked: fall back to the media query */ }
})();
