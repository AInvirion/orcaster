// "updated Ns ago" counter for the public status page.
//
// It lives in a separate file (not inline) so the CSP of the /status/* plane
// can use `script-src 'self'` without `'unsafe-inline'` or fragile hashes: the
// hash of an inline <script> breaks on any whitespace change in the template.
// Loading the script from the same origin is the cleanest option.
//
// The counter restarts on every partial swap performed by HTMX.
(function () {
  var last = Date.now();
  function fmt(s) { return s < 1 ? "updated just now" : "updated " + s + "s ago"; }
  function tick() {
    var el = document.getElementById("updated-ago");
    if (el) el.textContent = fmt(Math.round((Date.now() - last) / 1000));
  }
  document.body.addEventListener("htmx:afterSwap", function () { last = Date.now(); tick(); });
  setInterval(tick, 1000);
})();

// Version lens: progressive enhancement of the variant selector. Without JS,
// the <form method="get"> + Apply button navigate on selection. With JS, the
// button is hidden and the form is submitted when the <select> changes (one
// click less). External (not inline) to respect the `script-src 'self'` CSP of
// the public plane.
(function () {
  var sel = document.getElementById("status-variant");
  if (!sel) return;
  var form = sel.form;
  if (!form) return;
  var apply = form.querySelector(".variant-apply");
  if (apply) apply.hidden = true;
  sel.addEventListener("change", function () { form.submit(); });
})();
