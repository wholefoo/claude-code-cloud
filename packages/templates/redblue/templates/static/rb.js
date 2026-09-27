/* RedBlue site script: cookieless analytics beacon, goal clicks, theme toggle.
   No third-party code, no cookies, no fingerprinting. */
(function () {
  "use strict";
  var d = document, w = window;

  function send(payload) {
    try {
      var body = JSON.stringify(payload);
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/_rb/beacon", new Blob([body], { type: "application/json" }));
      } else {
        fetch("/_rb/beacon", { method: "POST", body: body, keepalive: true,
          headers: { "Content-Type": "application/json" } });
      }
    } catch (e) { /* analytics must never break the page */ }
  }

  function experiments() {
    try { return JSON.parse(d.body.getAttribute("data-rb-exp") || "{}"); } catch (e) { return {}; }
  }

  if (navigator.doNotTrack !== "1" && w.location.protocol !== "file:") {
    send({ t: "pageview", p: w.location.pathname, q: w.location.search, r: d.referrer, e: experiments() });
  }

  d.addEventListener("click", function (ev) {
    var el = ev.target.closest && ev.target.closest("[data-rb-goal]");
    if (el) send({ t: "goal", g: el.getAttribute("data-rb-goal"), p: w.location.pathname, e: experiments() });
  });
  d.addEventListener("htmx:afterRequest", function (ev) {
    var form = ev.detail && ev.detail.elt;
    if (form && form.matches && form.matches("form.rb-form") && ev.detail.successful) {
      send({ t: "goal", g: "form_" + (form.getAttribute("action") || "").split("/").pop(), p: w.location.pathname, e: experiments() });
    }
  });

  var root = d.documentElement, key = "rb-theme";
  try { var saved = localStorage.getItem(key); if (saved) root.setAttribute("data-theme", saved); } catch (e) {}
  d.addEventListener("click", function (ev) {
    if (!ev.target.closest || !ev.target.closest("[data-rb-theme]")) return;
    var dark = root.getAttribute("data-theme") === "dark" ||
      (!root.getAttribute("data-theme") && w.matchMedia("(prefers-color-scheme: dark)").matches);
    var next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem(key, next); } catch (e) {}
  });
})();
