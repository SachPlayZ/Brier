/* Landing page behaviour: nav, scroll reveal, tabs, count-up, block-wave canvas, glyph.
   No scroll listeners: everything is IntersectionObserver, CSS, or event-driven
   (the tab timer is the CSS animation's own `animationend`). The theme toggle lives in
   app.js; preloader, top bar and CTA pending state live in loading.js. Every motion
   here collapses to a static frame under prefers-reduced-motion. */
(function () {
  "use strict";

  var root = document.documentElement;
  var calm = window.matchMedia("(prefers-reduced-motion: reduce)");
  var hasIO = "IntersectionObserver" in window;
  function motionOK() { return !calm.matches; }

  /* ------------------------------------------------------------------ nav */
  /* Solid background once a 1px sentinel at the top of the page leaves the viewport. */
  var nav = document.getElementById("nav");
  var sentinel = document.getElementById("nav-sentinel");
  if (nav && sentinel && hasIO) {
    new IntersectionObserver(function (entries) {
      nav.classList.toggle("is-solid", !entries[0].isIntersecting);
    }).observe(sentinel);
  }
  var toggle = document.querySelector("[data-nav-toggle]");
  function setMenu(open) {
    if (!nav || !toggle) return;
    nav.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  }
  if (toggle) {
    toggle.addEventListener("click", function () { setMenu(!nav.classList.contains("is-open")); });
    nav.querySelectorAll(".nav-menu a").forEach(function (a) { a.addEventListener("click", function () { setMenu(false); }); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") setMenu(false); });
  }

  /* --------------------------------------------------------------- reveal */
  var reveals = document.querySelectorAll(".reveal");
  if (hasIO && motionOK()) {
    var ro = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.classList.add("is-in");
        ro.unobserve(e.target);
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
    reveals.forEach(function (el) { ro.observe(el); });
  } else {
    reveals.forEach(function (el) { el.classList.add("is-in"); });
  }

  /* -------------------------------------------------------------- count-up */
  /* The final number is in the HTML, so no-JS and reduced motion already show it. With
     motion allowed it runs once, from 0, when the stat scrolls into view. */
  var counters = document.querySelectorAll("[data-count]");
  function format(el, value) {
    return value.toFixed(parseInt(el.dataset.decimals || "0", 10)) + (el.dataset.suffix || "");
  }
  if (hasIO && motionOK() && counters.length) {
    var co = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        var el = e.target, end = parseFloat(el.dataset.count), t0 = performance.now(), dur = 1200;
        co.unobserve(el);
        (function step(now) {
          var p = Math.min((now - t0) / dur, 1);
          el.textContent = format(el, end * (1 - Math.pow(1 - p, 3)));
          if (p < 1) requestAnimationFrame(step); else el.textContent = format(el, end);
        })(t0);
      });
    }, { threshold: 0.6 });
    counters.forEach(function (el) { el.textContent = format(el, 0); co.observe(el); });
  }

  /* ----------------------------------------------------------------- tabs */
  /* Auto-advance is the progress bar's own CSS animation: when it ends, go to the next
     tab. Hover, focus or scrolling away pauses it; reduced motion never starts it. */
  var tabsEl = document.querySelector("[data-tabs]");
  if (tabsEl) {
    var tabs = Array.prototype.slice.call(tabsEl.querySelectorAll('[role="tab"]'));
    var panes = Array.prototype.slice.call(document.querySelectorAll('[role="tabpanel"]'));
    var current = 0;
    var select = function (i) {
      current = (i + tabs.length) % tabs.length;
      tabs.forEach(function (t, k) {
        t.setAttribute("aria-selected", k === current ? "true" : "false");
        t.tabIndex = k === current ? 0 : -1;
        var bar = t.querySelector(".tab-progress > span");
        if (bar) { bar.style.animation = "none"; void bar.offsetWidth; bar.style.animation = ""; } /* restart */
      });
      panes.forEach(function (p, k) { p.classList.toggle("is-active", k === current); });
    };
    tabs.forEach(function (t, k) {
      t.addEventListener("click", function () { select(k); });
      t.addEventListener("keydown", function (e) {
        var to = e.key === "ArrowDown" || e.key === "ArrowRight" ? current + 1
               : e.key === "ArrowUp" || e.key === "ArrowLeft" ? current - 1 : null;
        if (to === null) return;
        e.preventDefault(); select(to); tabs[current].focus();
      });
    });
    if (motionOK()) {
      tabsEl.setAttribute("data-auto", "");
      tabsEl.addEventListener("animationend", function (e) {
        if (e.target.matches && e.target.matches(".tab-progress > span")) select(current + 1);
      });
      var hold = { hover: false, focus: false, away: true };
      var sync = function () { tabsEl.classList.toggle("is-paused", hold.hover || hold.focus || hold.away); };
      var area = tabsEl.closest(".how-layout") || tabsEl;
      area.addEventListener("mouseenter", function () { hold.hover = true; sync(); });
      area.addEventListener("mouseleave", function () { hold.hover = false; sync(); });
      area.addEventListener("focusin", function () { hold.focus = true; sync(); });
      area.addEventListener("focusout", function () { hold.focus = false; sync(); });
      if (hasIO) {
        new IntersectionObserver(function (entries) { hold.away = !entries[0].isIntersecting; sync(); }, { threshold: 0.35 }).observe(area);
      } else { hold.away = false; }
      sync();
    }
  }

  /* -------------------------------------------------------------- block wave */
  /* The animated background, after the reference's hero wave: shaded blocks (the reference uses
     the characters "█▓▒░"). Here each shade is a filled square that grows and gets more opaque
     with intensity, so it looks the same in every font and costs one fillRect per cell.
     Colour comes from CSS (--block-rgb, --block-a, --block-lift), so each theme sets its own: bright teal in
     dark mode, deep green in light mode. Drawn only while the canvas is on screen,
     the tab is visible and motion is allowed; otherwise a single static frame. ~24fps. */
  var SHADE_ALPHA = [0.22, 0.42, 0.68, 1];     /* light to dense: ░ ▒ ▓ █ */
  var SHADE_SIZE = [0.42, 0.62, 0.82, 0.96];   /* fraction of the cell */
  function startBlocks(canvas) {
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    var cell = 14, cols = 0, rows = 0, w = 0, h = 0, t = 2.4, styles = [];
    var running = false, visible = false, raf = 0, last = 0;
    canvas.__frames = 0;

    function readTheme() {
      var cs = getComputedStyle(canvas);
      var rgb = cs.getPropertyValue("--block-rgb").trim() || "45, 212, 191";
      var max = parseFloat(cs.getPropertyValue("--block-a")) || 0.4;
      var lift = parseFloat(cs.getPropertyValue("--block-lift")) || 0;   /* raises the light shades toward the dense one */
      styles = SHADE_ALPHA.map(function (a) { return "rgba(" + rgb + "," + (max * (a + (1 - a) * lift)).toFixed(3) + ")"; });
    }
    function resize() {
      var r = canvas.getBoundingClientRect();
      if (!r.width || !r.height) return;
      var dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      w = r.width; h = r.height;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      cell = w < 700 ? 18 : 14;
      cols = Math.ceil(w / cell); rows = Math.ceil(h / cell);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      readTheme();
      draw();
    }
    function draw() {
      if (!cols) return;
      ctx.clearRect(0, 0, w, h);
      var k = 14 / cell, current = 0;
      for (var y = 0; y < rows; y++) {
        for (var x = 0; x < cols; x++) {
          /* the reference's three overlapping waves */
          var a = Math.sin(x * k * 0.08 + t) * Math.cos(y * k * 0.12 + t * 0.5);
          var b = Math.sin(x * k * 0.05 - t * 0.7) * Math.sin(y * k * 0.08 + t * 0.3);
          var c = Math.cos((x + y) * k * 0.03 + t * 0.4);
          var level = Math.floor((((a + b + c) / 3 + 1) / 2) * 5);   /* 0 = empty, 1..4 = shades */
          if (level < 1) continue;
          if (level > 4) level = 4;
          if (level !== current) { ctx.fillStyle = styles[level - 1]; current = level; }
          var s = cell * SHADE_SIZE[level - 1], o = (cell - s) / 2;
          ctx.fillRect(x * cell + o, y * cell + o, s, s);
        }
      }
      canvas.__frames++;
    }
    canvas.__draw = draw;   /* test hook: lets a check time one frame */
    function frame(now) {
      if (!running) return;
      raf = requestAnimationFrame(frame);
      if (now - last < 41) return;             /* ~24fps */
      t += Math.min((now - last) / 1000, 0.1) * 1.6; last = now;
      draw();
    }
    function update() {
      var go = visible && !document.hidden && motionOK();
      if (go && !running) { running = true; last = performance.now(); raf = requestAnimationFrame(frame); }
      else if (!go && running) { running = false; cancelAnimationFrame(raf); }
    }
    resize();
    if (window.ResizeObserver) {
      var pending = 0;
      new ResizeObserver(function () { cancelAnimationFrame(pending); pending = requestAnimationFrame(resize); }).observe(canvas);
    }
    if (hasIO) {
      new IntersectionObserver(function (entries) { visible = entries[0].isIntersecting; update(); }).observe(canvas);
    }
    document.addEventListener("visibilitychange", update);
    calm.addEventListener("change", update);
    /* recolour when the theme is toggled */
    new MutationObserver(function () { readTheme(); if (!running) draw(); })
      .observe(root, { attributes: true, attributeFilter: ["data-bs-theme"] });
  }
  document.querySelectorAll("canvas[data-ascii]").forEach(startBlocks);

  /* ---------------------------------------------------------------- glyph */
  /* Two receipts converging. ASCII-only so the columns always line up in any mono font. */
  var LINK = ["  ..  ", " <..> ", " <--> ", " <==> "];
  function receipts(link) {
    return "+--------+      +--------+\n" +
           "| =====  |      | =====  |\n" +
           "| ===    |" + link + "| ===    |\n" +
           "| ====   |      | ====   |\n" +
           "+--------+      +--------+";
  }
  document.querySelectorAll("[data-glyph]").forEach(function (pre) {
    var n = LINK.length - 1, timer = 0, visible = false;
    function render() { pre.textContent = receipts(LINK[n]); }
    function update() {
      var go = visible && !document.hidden && motionOK();
      if (go && !timer) { timer = setInterval(function () { n = (n + 1) % LINK.length; render(); }, 520); }
      else if (!go && timer) { clearInterval(timer); timer = 0; n = LINK.length - 1; render(); }
    }
    render();
    if (hasIO) new IntersectionObserver(function (entries) { visible = entries[0].isIntersecting; update(); }).observe(pre);
    document.addEventListener("visibilitychange", update);
    calm.addEventListener("change", update);
  });
})();
