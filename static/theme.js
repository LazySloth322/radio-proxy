/* private-radio-proxy - theme bootstrap.
 * Loaded synchronously in <head> (before the stylesheet renders) so the page
 * never flashes the wrong theme. CSP is script-src 'self', hence a real file
 * instead of an inline snippet. Also exposes window.RPTheme for app.js. */
(function () {
  "use strict";

  var KEY = "rp_theme";

  function stored() {
    try {
      var v = localStorage.getItem(KEY);
      return v === "light" || v === "dark" ? v : null;
    } catch (e) { return null; }  // storage can be blocked; degrade quietly
  }

  function systemTheme() {
    try {
      return window.matchMedia &&
             window.matchMedia("(prefers-color-scheme: light)").matches
             ? "light" : "dark";
    } catch (e) { return "dark"; }
  }

  function current() {
    var t = stored() || systemTheme();
    document.documentElement.setAttribute("data-theme", t);
    return t;
  }

  function set(theme) {
    if (theme !== "light" && theme !== "dark") theme = "dark";
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem(KEY, theme); } catch (e) { /* private mode */ }
  }

  window.RPTheme = { current: current, set: set };
  current();
})();
