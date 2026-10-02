// Drive Sync — interfaz. Habla con el servicio a través de las rutas /api de esta app.
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); };
  var icon = function (n) { return '<svg class="ico"><use href="#i-' + n + '"/></svg>'; };

  function api(method, path, body) {
    var opts = { method: method, headers: {} };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, status: r.status, body: j }; });
    }).catch(function () { return { ok: false, status: 0, body: { error: "sin respuesta" } }; });
  }

  function ago(ms) {
    if (!ms) { return "todavía no"; }
    var s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 10) { return "ahora mismo"; }
    if (s < 60) { return "hace " + s + " s"; }
    if (s < 3600) { return "hace " + Math.round(s / 60) + " min"; }
    if (s < 86400) { return "hace " + Math.round(s / 3600) + " h"; }
    return "hace " + Math.round(s / 86400) + " d";
  }
  function clock(ms) { return new Date(ms).toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" }); }
  function base(p) { return String(p).split("/").filter(Boolean).pop() || p; }

  // ------------------------------------------------------- diálogos propios
  function confirmDialog(title, text, yes) {
    return new Promise(function (resolve) {
      var d = $("dlg-confirm");
      $("confirm-title").textContent = title; $("confirm-text").textContent = text; $("confirm-yes").textContent = yes || "Aceptar";
      var done = function (v) { d.close(); resolve(v); };
      $("confirm-yes").onclick = function () { done(true); };
      $("confirm-no").onclick = function () { done(false); };
      d.showModal();
    });
  }
  function promptDialog(title, ok) {
    return new Promise(function (resolve) {
      var d = $("dlg-prompt"), input = $("prompt-input");
      $("prompt-title").textContent = title; $("prompt-yes").textContent = ok || "Aceptar"; input.value = "";
      var done = function (v) { d.close(); resolve(v); };
      $("prompt-yes").onclick = function () { done(input.value.trim()); };
      $("prompt-no").onclick = function () { done(null); };
      input.onkeydown = function (e) { if (e.key === "Enter") { done(input.value.trim()); } };
      d.showModal(); input.focus();
    });
  }

  // ------------------------------------------------------------- estado
  var state = { status: null };
  var showSettings = false;

  function chip(link) {
    var s = link.enabled ? link.status : "en pausa";
    var cls = { "al día": "ok", "sincronizando": "busy", "sin conexión": "warn", "con errores": "err", "carpeta no encontrada": "err" }[s] || "";
    return '<span class="chip ' + cls + '">' + esc(s) + "</span>";
  }

  function renderLinks(links) {
    $("empty").hidden = links.length > 0;
    $("links").innerHTML = links.map(function (l) {
      return '<div class="link" data-id="' + l.id + '">' +
        '<div class="ends"><div class="end"><b>' + icon("folder") + esc(base(l.local_path)) + '</b><span class="path" title="' + esc(l.local_path) + '">' + esc(l.local_path) + "</span></div>" +
        '<span class="arrows">' + icon("arrows") + "</span>" +
        '<div class="end"><b>' + icon("cloud") + esc(l.remote_name || "Drive") + '</b><span class="path">Carpeta de Drive</span></div></div>' +
        '<div class="meta">' + chip(l) + '<span class="muted">Última sincronización: ' + ago(l.last_sync) + "</span><span class=\"spacer\"></span>" +
        '<button class="ghost small" data-act="open">' + icon("open") + "Abrir</button>" +
        '<button class="ghost small" data-act="toggle">' + icon(l.enabled ? "pause" : "play") + (l.enabled ? "Pausar" : "Reanudar") + "</button>" +
        '<button class="ghost small danger" data-act="remove">' + icon("trash") + "Quitar</button></div>" +
        (l.last_error ? '<div class="err-text">' + esc(l.last_error) + "</div>" : "") + "</div>";
    }).join("");
  }

  function render() {
    var s = state.status, banner = $("banner");
    if (!s || s.error && s.configured === undefined) {
      banner.hidden = false; banner.textContent = (s && s.error) || "Conectando con el servicio…";
      $("onboarding").hidden = true; $("links-section").hidden = true; $("conn").innerHTML = "";
      return;
    }
    banner.hidden = true;
    var needSetup = !s.configured || showSettings;
    $("onboarding").hidden = !needSetup;
    $("links-section").hidden = needSetup;
    $("settings-cancel").hidden = !s.configured;
    $("conn").innerHTML = s.configured
      ? '<span class="dot ' + (s.online ? "on" : "off") + '"></span>' + (s.online ? "Conectado como <b>" + esc(s.user) + "</b> · " : "Sin conexión con ") + esc(s.server_url)
      : "";
    if (!needSetup) { renderLinks(s.links); }
  }

  function refresh() {
    return api("GET", "/api/status").then(function (r) { state.status = r.ok ? r.body : { error: r.body.error || "El servicio no responde." }; render(); });
  }
  function refreshEvents() {
    api("GET", "/api/events").then(function (r) {
      if (!r.ok || !Array.isArray(r.body)) { return; }
      $("events").innerHTML = r.body.slice(0, 12).map(function (e) {
        return '<li><time>' + clock(e.ts) + '</time><span class="l-' + esc(e.level) + '">' + esc(e.message) + "</span></li>";
      }).join("") || '<li class="muted">Sin actividad todavía.</li>';
    });
  }

  // ---------------------------------------------------------------- ajustes
  $("btn-settings").onclick = function () {
    showSettings = true;
    if (state.status) { $("server_url").value = state.status.server_url || ""; }
    $("token").value = ""; $("settings-error").hidden = true; render();
  };
  $("settings-cancel").onclick = function () { showSettings = false; render(); };
  $("form-settings").onsubmit = function (e) {
    e.preventDefault();
    var btn = e.target.querySelector("button[type=submit]"); btn.disabled = true;
    api("POST", "/api/settings", { server_url: $("server_url").value, token: $("token").value }).then(function (r) {
      btn.disabled = false;
      if (!r.ok) { $("settings-error").hidden = false; $("settings-error").textContent = r.body.error || "No se pudo conectar."; return; }
      showSettings = false; $("settings-error").hidden = true; refresh();
    });
  };
  $("btn-sync").onclick = function () { api("POST", "/api/sync").then(function () { setTimeout(refresh, 800); }); };

  // ------------------------------------------------------- acciones por carpeta
  $("links").onclick = function (e) {
    var btn = e.target.closest("button[data-act]"); if (!btn) { return; }
    var id = btn.closest(".link").dataset.id;
    var link = state.status.links.filter(function (l) { return String(l.id) === id; })[0];
    if (btn.dataset.act === "open") { api("POST", "/api/open", { path: link.local_path }); }
    if (btn.dataset.act === "toggle") { api("POST", "/api/links/" + id + "/enabled", { enabled: !link.enabled }).then(refresh); }
    if (btn.dataset.act === "remove") {
      confirmDialog("¿Dejar de sincronizar esta carpeta?", "No se borra nada, ni en este equipo ni en Drive: simplemente dejan de estar enlazadas.", "Quitar enlace")
        .then(function (yes) { if (yes) { api("DELETE", "/api/links/" + id).then(refresh); } });
    }
  };

  // ------------------------------------------------------- añadir carpeta
  var add = { local: "", stack: [{ id: "", name: "Drive" }] };
  function crumbs() {
    $("crumbs").innerHTML = add.stack.map(function (c, i) {
      return (i ? icon("chev") : "") + '<button type="button" data-i="' + i + '">' + esc(c.name) + "</button>";
    }).join("");
  }
  function loadFolders() {
    crumbs();
    var cur = add.stack[add.stack.length - 1];
    $("folders").innerHTML = '<li class="none">Cargando…</li>';
    api("GET", "/api/remote/folders?parent_id=" + encodeURIComponent(cur.id)).then(function (r) {
      if (!r.ok) { $("folders").innerHTML = '<li class="none">' + esc(r.body.error || "No se pudieron leer las carpetas") + "</li>"; return; }
      $("folders").innerHTML = r.body.folders.map(function (f) {
        return '<li data-id="' + esc(f.id) + '" data-name="' + esc(f.name) + '">' + icon("folder") + esc(f.name) + "</li>";
      }).join("") || '<li class="none">Sin subcarpetas</li>';
    });
  }
  $("crumbs").onclick = function (e) { var b = e.target.closest("button"); if (b) { add.stack = add.stack.slice(0, +b.dataset.i + 1); loadFolders(); } };
  $("folders").onclick = function (e) { var li = e.target.closest("li[data-id]"); if (li) { add.stack.push({ id: li.dataset.id, name: li.dataset.name }); loadFolders(); } };
  $("new-remote").onclick = function () {
    promptDialog("Nueva carpeta en Drive", "Crear").then(function (name) {
      if (!name) { return; }
      api("POST", "/api/remote/folders", { parent_id: add.stack[add.stack.length - 1].id, name: name }).then(function (r) {
        if (r.ok) { loadFolders(); } else { $("add-error").hidden = false; $("add-error").textContent = r.body.error; }
      });
    });
  };
  $("btn-add").onclick = function () {
    add = { local: "", stack: [{ id: "", name: "Drive" }] };
    $("local-path").textContent = "ninguna elegida"; $("add-ok").disabled = true; $("add-error").hidden = true;
    $("dlg-add").showModal(); loadFolders();
  };
  $("pick-local").onclick = function () {
    api("POST", "/api/pick-folder").then(function (r) {
      if (r.ok && r.body.path) { add.local = r.body.path; $("local-path").textContent = add.local; $("local-path").title = add.local; $("add-ok").disabled = false; }
    });
  };
  $("add-cancel").onclick = function () { $("dlg-add").close(); };
  $("add-ok").onclick = function () {
    var cur = add.stack[add.stack.length - 1];
    var link = function (id, name) {
      return api("POST", "/api/links", { local_path: add.local, remote_id: id, remote_name: name }).then(function (r) {
        if (!r.ok) { $("add-error").hidden = false; $("add-error").textContent = r.body.error || "No se pudo enlazar."; return; }
        $("dlg-add").close(); refresh();
      });
    };
    $("add-ok").disabled = true;
    var done = function () { $("add-ok").disabled = !add.local; };
    if ($("make-subfolder").checked) {
      api("POST", "/api/remote/folders", { parent_id: cur.id, name: base(add.local) }).then(function (r) {
        if (!r.ok) { $("add-error").hidden = false; $("add-error").textContent = r.body.error; done(); return; }
        link(r.body.id, r.body.name).then(done);
      });
    } else { link(cur.id, cur.name).then(done); }
  };

  api("POST", "/api/chrome");
  refresh(); refreshEvents();
  setInterval(refresh, 2000); setInterval(refreshEvents, 4000);
})();
