/* Loading states: preloader, navigation progress bar, delayed skeleton, button
   pending state, image fade-in, and one screen-reader announcer.
   Timings live in loading.css (:root); this file reads them so there is one place to tune.
   JS only toggles classes and CSS variables. Every animation itself runs in CSS. */
(function () {
  "use strict";

  var root = document.documentElement;
  var storage = {
    get: function (k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* private mode */ } },
    del: function (k) { try { sessionStorage.removeItem(k); } catch (e) { /* private mode */ } }
  };

  /* Read a duration such as "350ms" or "1.4s" from a CSS custom property. */
  function cssMs(name, fallback) {
    var raw = getComputedStyle(root).getPropertyValue(name).trim();
    var n = parseFloat(raw);
    if (isNaN(n)) return fallback;
    return /ms$/.test(raw) ? n : raw.slice(-1) === "s" ? n * 1000 : n;
  }

  /* --------------------------------------------------------- announcer */
  /* One polite live region. Spinners are aria-hidden, so a screen reader hears a
     single short message per state change instead of repeated noise. */
  var live = document.getElementById("sr-status");
  function announce(message) {
    if (!live) return;
    live.textContent = "";
    setTimeout(function () { live.textContent = message; }, 50);
  }

  /* --------------------------------------------------------- preloader */
  function initPreloader() {
    var el = document.getElementById("preloader");
    if (!el) return;
    if (!root.classList.contains("has-preloader")) { el.remove(); return; }

    var min = cssMs("--load-min", 350);
    var cap = cssMs("--load-cap", 1500);
    var started = performance.now();
    var left = false;

    requestAnimationFrame(function () {
      requestAnimationFrame(function () { el.classList.add("is-running"); });
    });

    function remove() {
      root.classList.remove("has-preloader");
      el.remove();
    }
    function leave() {
      if (left) return;
      left = true;
      el.classList.add("is-done");
      setTimeout(function () {
        el.classList.add("is-leaving");
        el.addEventListener("transitionend", remove, { once: true });
        setTimeout(remove, 600); /* if transitionend never fires */
      }, 150);
    }

    var loaded = document.readyState === "complete"
      ? Promise.resolve()
      : new Promise(function (r) { window.addEventListener("load", r, { once: true }); });
    var fonts = document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve();
    Promise.all([loaded, fonts]).then(function () {
      setTimeout(leave, Math.max(0, min - (performance.now() - started)));
    });
    /* Hard cap, measured from navigation start (performance.now() is 0 there), not from
       when this script happened to run: a slow CDN must not stretch the wait. */
    setTimeout(leave, Math.max(0, cap - performance.now()));
  }

  /* ---------------------------------------------- navigation progress bar */
  var barEl = document.getElementById("nav-bar");
  var barTimer = null;
  var barValue = 0;

  function barSet(p) { barValue = p; if (barEl) barEl.style.setProperty("--p", p); }
  function barReset() {
    clearInterval(barTimer);
    barTimer = null;
    if (!barEl) return;
    barEl.classList.remove("is-active", "is-finishing", "no-anim");
    barSet(0);
  }
  function barStart() {
    if (!barEl || barTimer) return;
    barEl.classList.remove("is-finishing");
    barEl.classList.add("is-active");
    barSet(0.08);
    /* Decaying steps toward 80%: it always moves, and never claims to be done. */
    barTimer = setInterval(function () { barSet(barValue + (0.8 - barValue) * 0.1); }, 300);
    storage.set("navPending", "1");
  }
  /* The previous page started the bar; the new page completes it. */
  function barFinish() {
    if (!barEl || storage.get("navPending") !== "1") return;
    storage.del("navPending");
    barEl.classList.add("is-active", "no-anim");
    barSet(0.6);
    void barEl.offsetWidth; /* commit the start value before animating to 100% */
    barEl.classList.remove("no-anim");
    barSet(1);
    setTimeout(function () { barEl.classList.add("is-finishing"); }, 300);
    setTimeout(barReset, 650);
  }

  /* ------------------------------------------------------------ skeleton */
  var ROUTES = [
    [/^\/$/, "cards"],
    [/^\/claims\/new\/?$/, "form"],
    [/^\/claims\/?$/, "table"],
    [/^\/review\/?$/, "table"],
    [/^\/claims\/[^/]+\/?$/, "detail"],
    [/^\/duplicates\/\d+\/?$/, "detail"]
  ];
  var skTimer = null;
  var skFailsafe = null;

  function variantFor(url) {
    for (var i = 0; i < ROUTES.length; i++) if (ROUTES[i][0].test(url.pathname)) return ROUTES[i][1];
    return null; /* unknown route (login, admin): no skeleton, just the bar */
  }
  function skeletonClear() {
    clearTimeout(skTimer);
    clearTimeout(skFailsafe);
    var main = document.querySelector("main.page");
    if (!main) return;
    main.classList.remove("is-pending");
    main.removeAttribute("aria-busy");
    var host = main.querySelector(":scope > .sk-host");
    if (host) host.remove();
  }
  /* Only after the delay: a fast navigation should never flash a skeleton. */
  function skeletonSoon(url) {
    var variant = variantFor(url);
    var main = document.querySelector("main.page");
    var tpl = variant && document.querySelector('template[data-skeleton="' + variant + '"]');
    if (!tpl || !main) return;
    clearTimeout(skTimer);
    skTimer = setTimeout(function () {
      var host = document.createElement("div");
      host.className = "sk-host";
      host.setAttribute("aria-hidden", "true");
      host.appendChild(tpl.content.cloneNode(true));
      main.classList.add("is-pending");
      main.setAttribute("aria-busy", "true");
      main.appendChild(host);
      announce("Loading");
      /* If the navigation never completes (cancelled, download), restore the page. */
      skFailsafe = setTimeout(function () { skeletonClear(); barReset(); }, 15000);
    }, cssMs("--skeleton-delay", 250));
  }

  /* ---------------------------------------------------- link and form hooks */
  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest && e.target.closest("a[href]");
    if (!a || (a.target && a.target !== "_self") || a.hasAttribute("download")) return;
    if (a.hasAttribute("data-bs-toggle")) return;
    var url = new URL(a.href, location.href);
    if (url.origin !== location.origin) return;
    if (url.pathname === location.pathname && url.search === location.search) return; /* hash or self */
    barStart();
    skeletonSoon(url);
    if (a.hasAttribute("data-loading-label")) {
      a.classList.add("is-loading");
      a.setAttribute("aria-busy", "true");
      announce(a.getAttribute("data-loading-label"));
    }
  });

  /* ------------------------------------------------------- button pending */
  /* Wrap each submit button's content once, up front, so the click itself causes no
     layout change: label stays in flow (hidden while loading), spinner overlays it. */
  function prepareButton(btn) {
    if (btn.querySelector(":scope > .btn-label")) return;
    var label = document.createElement("span");
    label.className = "btn-label";
    while (btn.firstChild) label.appendChild(btn.firstChild);
    btn.appendChild(label);
    var sp = document.createElement("span");
    sp.className = "btn-spinner";
    sp.setAttribute("aria-hidden", "true");
    sp.innerHTML = '<span class="spinner"></span><span class="dots"><i></i><i></i><i></i></span>';
    btn.appendChild(sp);
  }
  function submitButtons(form) {
    return form.querySelectorAll('button[type="submit"], button:not([type])');
  }
  document.querySelectorAll('form[method="post" i]').forEach(function (form) {
    submitButtons(form).forEach(prepareButton);
  });
  /* Links styled as buttons (landing CTAs) get the same overlay: they open another page, so
     the click needs acknowledging too. */
  document.querySelectorAll("a[data-loading-label]").forEach(prepareButton);

  function pendingStart(form, submitter) {
    form.dataset.pending = "1";
    form.setAttribute("aria-busy", "true");
    /* aria-disabled, never `disabled`: a disabled submitter is left out of the form
       data, which would drop name="action" value="approve" from the decision form. */
    submitButtons(form).forEach(function (b) {
      b.setAttribute("aria-disabled", "true");
      b.classList.add("is-disabled");
    });
    var btn = submitter || submitButtons(form)[0];
    if (!btn) return;
    btn.classList.remove("is-disabled");
    btn.classList.add("is-loading");
    btn.setAttribute("aria-busy", "true");
    announce(btn.getAttribute("data-loading-label") || "Working");
  }
  function pendingClear() {
    document.querySelectorAll("a.is-loading").forEach(function (a) {
      a.classList.remove("is-loading");
      a.removeAttribute("aria-busy");
    });
    document.querySelectorAll("form[data-pending]").forEach(function (form) {
      delete form.dataset.pending;
      form.removeAttribute("aria-busy");
      form.querySelectorAll(".is-loading, .is-disabled").forEach(function (b) {
        b.classList.remove("is-loading", "is-disabled");
        b.removeAttribute("aria-disabled");
        b.removeAttribute("aria-busy");
      });
    });
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.pending) { e.preventDefault(); return; } /* double submit */
    if (e.defaultPrevented) return;
    var method = (form.getAttribute("method") || "get").toLowerCase();
    barStart();
    if (method === "post") {
      pendingStart(form, e.submitter);
    } else {
      /* A GET form (filters) lands on a known list page; a POST result is unknown and
         hiding what the user just typed would be worse than the button spinner alone. */
      skeletonSoon(new URL(form.action || location.href, location.href));
    }
  });

  /* ------------------------------------------------------------- images */
  /* Loaded/error state lives on each <img>; the frame (shimmer, fallback) mirrors the one that
     is currently visible. That matters for themed pairs: toggling the theme reveals an image
     that has not loaded yet, so the shimmer must come back until it does. */
  var frames = document.querySelectorAll(".img-frame");
  function visibleImg(frame) {
    return frame.querySelector('img[data-theme="' + root.getAttribute("data-bs-theme") + '"]') || frame.querySelector("img");
  }
  function syncFrame(frame) {
    var img = visibleImg(frame);
    if (!img) return;
    var failed = img.classList.contains("is-error");
    frame.classList.toggle("is-loaded", img.classList.contains("is-loaded"));
    frame.classList.toggle("is-error", failed);
    var note = frame.querySelector(".img-fallback");
    if (failed && !note) {
      note = document.createElement("div");
      note.className = "img-fallback";
      note.innerHTML = '<i class="ph ph-image" aria-hidden="true"></i><span>Image unavailable</span>';
      frame.appendChild(note);
    } else if (!failed && note) {
      note.remove();
    }
  }
  function initImages() {
    frames.forEach(function (frame) {
      frame.querySelectorAll("img").forEach(function (img) {
        function ok() { img.classList.add("is-loaded"); syncFrame(frame); }
        function bad() { img.classList.add("is-error"); syncFrame(frame); }
        /* The image may already have finished (cached) or failed before this script ran, and
           those events are gone. `complete` is true for both; a lazy image that has not
           started yet (or is display:none) reports false, so it falls through to the listeners. */
        if (img.complete) { (img.naturalWidth > 0 ? ok : bad)(); return; }
        img.addEventListener("load", ok, { once: true });
        img.addEventListener("error", bad, { once: true });
      });
      syncFrame(frame);
    });
    if (frames.length) {
      new MutationObserver(function () { frames.forEach(syncFrame); })
        .observe(root, { attributes: true, attributeFilter: ["data-bs-theme"] });
    }
  }

  /* -------------------------------------------------- lifecycle and Back */
  /* Back/forward can restore the page from bfcache in its pending state. */
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    skeletonClear();
    pendingClear();
    barReset();
    storage.del("navPending");
  });

  initPreloader();
  initImages();
  barFinish();
})();
