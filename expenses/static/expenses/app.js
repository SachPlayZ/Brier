/* Theme toggle and mobile sidebar. Loading states live in loading.js.
   The initial theme is applied by an inline snippet in <head> so there is no flash. */
(function () {
  "use strict";
  var root = document.documentElement;

  function storeTheme(value) {
    try { localStorage.setItem("theme", value); } catch (e) { /* private mode: ignore */ }
  }

  document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var next = root.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-bs-theme", next);
      storeTheme(next);
    });
  });

  var sidebar = document.getElementById("sidebar");
  var scrim = document.getElementById("scrim");
  function setMenu(open) {
    if (!sidebar || !scrim) return;
    sidebar.classList.toggle("is-open", open);
    scrim.classList.toggle("is-open", open);
    document.querySelectorAll("[data-menu-toggle]").forEach(function (b) {
      b.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
  document.querySelectorAll("[data-menu-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () { setMenu(!sidebar.classList.contains("is-open")); });
  });
  if (scrim) scrim.addEventListener("click", function () { setMenu(false); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") setMenu(false); });
})();
