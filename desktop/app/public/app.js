// Drive — interfaz de escritorio: explorador de Drive y sincronización. Habla con el servicio a través de las rutas /api de esta app.
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
  var tab = "files", filesLoaded = false;

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
      $("onboarding").hidden = true; $("links-section").hidden = true; $("files-section").hidden = true; $("tabs").hidden = true; $("conn").innerHTML = "";
      return;
    }
    banner.hidden = true;
    var needSetup = !s.configured || showSettings;
    $("onboarding").hidden = !needSetup;
    $("tabs").hidden = needSetup;
    $("links-section").hidden = needSetup || tab !== "sync";
    $("files-section").hidden = needSetup || tab !== "files";
    if (!needSetup && tab === "files" && !filesLoaded) { filesLoaded = true; loadFiles(); }
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

  // ------------------------------------------------------------- pestañas
  $("tabs").onclick = function (e) {
    var b = e.target.closest("button[data-tab]"); if (!b) { return; }
    tab = b.dataset.tab;
    [].forEach.call($("tabs").children, function (x) { x.classList.toggle("on", x === b); });
    render();
  };

  // ------------------------------------------------- explorador de Drive
  function size(n) {
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0, v = +n || 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return i ? v.toFixed(1) + " " + u[i] : v + " B";
  }
  // El servidor da fechas UTC sin zona.
  function when(iso) { return iso ? ago(Date.parse(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + "Z")) : ""; }
  var files = { stack: [{ id: "", name: "Mi unidad" }] };
  function msg(text, isError) {
    var m = $("f-msg"); m.hidden = !text; m.textContent = text || ""; m.className = isError ? "error" : "muted";
  }
  function loadFiles() {
    var cur = files.stack[files.stack.length - 1];
    $("f-crumbs").innerHTML = files.stack.map(function (c, i) {
      return (i ? icon("chev") : "") + '<button type="button" data-i="' + i + '">' + esc(c.name) + "</button>";
    }).join("");
    api("GET", "/api/remote/list?parent_id=" + encodeURIComponent(cur.id)).then(function (r) {
      if (!r.ok) { $("f-rows").innerHTML = '<tr class="none"><td colspan="4">' + esc(r.body.error || "No se pudo leer Drive") + "</td></tr>"; return; }
      $("f-rows").innerHTML = r.body.items.map(function (f) {
        return '<tr class="' + (f.is_dir ? "dir" : "file") + '" data-id="' + esc(f.id) + '" data-name="' + esc(f.name) + '">' +
          '<td><span class="name">' + icon(f.is_dir ? "folder" : "file") + esc(f.name) + "</span></td>" +
          '<td class="num-col muted">' + size(f.size) + "</td>" +
          '<td class="muted">' + esc(when(f.updated_at)) + "</td>" +
          '<td class="num-col">' + (f.is_dir ? "" : '<button class="ghost small icon" data-act="download" title="Descargar" aria-label="Descargar">' + icon("download") + "</button>") + "</td></tr>";
      }).join("") || '<tr class="none"><td colspan="4">Carpeta vacía</td></tr>';
    });
  }
  $("f-crumbs").onclick = function (e) { var b = e.target.closest("button"); if (b) { files.stack = files.stack.slice(0, +b.dataset.i + 1); msg(""); loadFiles(); } };
  function download(tr) {
    msg("Descargando «" + tr.dataset.name + "»…");
    api("POST", "/api/remote/download", { id: tr.dataset.id, name: tr.dataset.name }).then(function (r) {
      if (r.body.cancelled) { msg(""); return; }
      msg(r.ok ? "Descargado en " + r.body.path : (r.body.error || "No se pudo descargar."), !r.ok);
    });
  }
  function openDir(tr) { files.stack.push({ id: tr.dataset.id, name: tr.dataset.name }); msg(""); loadFiles(); }
  $("f-rows").onclick = function (e) {
    var tr = e.target.closest("tr[data-id]"); if (!tr) { return; }
    if (e.target.closest("button[data-act=download]")) { download(tr); return; }
    if (tr.classList.contains("dir")) { openDir(tr); }
  };

  // ------------------------------------------------ menú del clic derecho
  // La app se queda el clic derecho: sobre un fichero o carpeta abre este menú
  // y en el resto no hace nada (nada de «Recargar» ni «Atrás» de WebKit). En
  // los campos de texto se deja el nativo para poder pegar el token.
  var ctx = $("ctx"), ctxRow = null;
  function closeCtx() {
    ctx.hidden = true;
    if (ctxRow) { ctxRow.classList.remove("sel"); ctxRow = null; }
  }
  // Un token sin el ámbito «share» es el fallo esperable: se explica qué hacer.
  function shareError(r, fallback) {
    var e = r.body.error || fallback;
    return /ámbito 'share'/.test(e) ? "Tu token no puede compartir: crea uno con el ámbito share en la web (Perfil → Tokens de API) y pégalo en Ajustes." : e;
  }
  var ctxActions = {
    open: openDir,
    download: download,
    web: function (tr) { api("POST", "/api/open-web", { id: tr.dataset.id }); },
    link: function (tr) {
      msg("Creando enlace…");
      api("POST", "/api/remote/link", { id: tr.dataset.id }).then(function (r) {
        msg(r.ok ? "Enlace copiado al portapapeles: " + r.body.url : shareError(r, "No se pudo crear el enlace."), !r.ok);
      });
    },
    share: function (tr) {
      var d = $("dlg-share");
      $("share-title").textContent = "Compartir «" + tr.dataset.name + "»";
      $("share-user").value = ""; $("share-error").hidden = true;
      $("form-share").onsubmit = function (e) {
        e.preventDefault();
        var user = $("share-user").value.trim();
        api("POST", "/api/remote/share", { id: tr.dataset.id, username: user, permission: $("share-perm").value }).then(function (r) {
          if (!r.ok) { $("share-error").hidden = false; $("share-error").textContent = shareError(r, "No se pudo compartir."); return; }
          d.close(); msg("«" + tr.dataset.name + "» compartido con " + user + ".");
        });
      };
      $("share-no").onclick = function () { d.close(); };
      d.showModal(); $("share-user").focus();
    }
  };
  document.addEventListener("contextmenu", function (e) {
    if (e.target.closest("input, textarea")) { return; }
    e.preventDefault();
    closeCtx();
    var tr = e.target.closest("#f-rows tr[data-id]"); if (!tr) { return; }
    var items = [tr.classList.contains("dir") ? ["open", "folder", "Abrir"] : ["download", "download", "Descargar"], null,
      ["link", "link", "Crear enlace para compartir"], ["share", "user", "Compartir con un usuario…"], null,
      ["web", "open", "Abrir en la web"]];
    ctx.innerHTML = items.map(function (it) {
      return it ? '<button type="button" role="menuitem" data-act="' + it[0] + '">' + icon(it[1]) + esc(it[2]) + "</button>" : "<hr>";
    }).join("");
    ctxRow = tr; tr.classList.add("sel");
    ctx.hidden = false;
    ctx.style.left = Math.min(e.clientX, window.innerWidth - ctx.offsetWidth - 8) + "px";
    ctx.style.top = Math.min(e.clientY, window.innerHeight - ctx.offsetHeight - 8) + "px";
    ctx.querySelector("button").focus();
  });
  ctx.onclick = function (e) {
    var b = e.target.closest("button[data-act]"), tr = ctxRow; if (!b) { return; }
    closeCtx(); ctxActions[b.dataset.act](tr);
  };
  document.addEventListener("click", function (e) { if (!ctx.contains(e.target)) { closeCtx(); } });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") { closeCtx(); } });
  window.addEventListener("scroll", closeCtx, true);
  window.addEventListener("resize", closeCtx);
  window.addEventListener("blur", closeCtx);
  $("f-upload").onclick = function () {
    var cur = files.stack[files.stack.length - 1];
    msg("Elige el fichero…");
    api("POST", "/api/remote/upload", { parent_id: cur.id }).then(function (r) {
      if (r.body.cancelled) { msg(""); return; }
      msg(r.ok ? "Subido." : (r.body.error || "No se pudo subir."), !r.ok);
      loadFiles();
    });
  };
  $("f-mkdir").onclick = function () {
    promptDialog("Nueva carpeta en Drive", "Crear").then(function (name) {
      if (!name) { return; }
      api("POST", "/api/remote/folders", { parent_id: files.stack[files.stack.length - 1].id, name: name }).then(function (r) {
        if (!r.ok) { msg(r.body.error || "No se pudo crear.", true); }
        loadFiles();
      });
    });
  };
  $("f-web").onclick = function () { api("POST", "/api/open-web", { id: files.stack[files.stack.length - 1].id }); };

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
  // Dos modos: «upload» crea en Drive una carpeta nueva (con el nombre de la
  // local) dentro de la elegida; «link» enlaza exactamente las dos elegidas.
  var add = { mode: "link", local: "", stack: [{ id: "", name: "Drive" }] };
  function summary() {
    var cur = add.stack[add.stack.length - 1];
    var local = add.local ? "«" + base(add.local) + "»" : "la carpeta local";
    $("add-summary").textContent = add.mode === "upload"
      ? "Se creará " + local + " dentro de «" + cur.name + "» en Drive y se enlazará con ella."
      : "Se enlazará " + local + " con «" + cur.name + "»" + (cur.id ? "" : " (todo tu Drive)") + ". Lo que falte en un lado se copia al otro.";
  }
  function crumbs() {
    $("crumbs").innerHTML = add.stack.map(function (c, i) {
      return (i ? icon("chev") : "") + '<button type="button" data-i="' + i + '">' + esc(c.name) + "</button>";
    }).join("");
  }
  function loadFolders() {
    crumbs(); summary();
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
  function openAdd(mode) {
    add = { mode: mode, local: "", stack: [{ id: "", name: "Drive" }] };
    var up = mode === "upload";
    $("add-title").textContent = up ? "Subir una carpeta" : "Enlazar carpetas";
    $("remote-label").textContent = up ? "Dónde crearla en Drive (entra en la carpeta)" : "Carpeta de Drive con la que enlazar (entra en ella)";
    $("add-ok").textContent = up ? "Subir y enlazar" : "Enlazar";
    $("local-path").textContent = "ninguna elegida"; $("add-ok").disabled = true; $("add-error").hidden = true;
    $("dlg-add").showModal(); loadFolders();
  }
  $("btn-upload").onclick = function () { openAdd("upload"); };
  $("btn-link").onclick = function () { openAdd("link"); };
  $("pick-local").onclick = function () {
    api("POST", "/api/pick-folder").then(function (r) {
      if (r.ok && r.body.path) { add.local = r.body.path; $("local-path").textContent = add.local; $("local-path").title = add.local; $("add-ok").disabled = false; summary(); }
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
    if (add.mode === "upload") {
      api("POST", "/api/remote/folders", { parent_id: cur.id, name: base(add.local) }).then(function (r) {
        if (!r.ok) { $("add-error").hidden = false; $("add-error").textContent = r.body.error; done(); return; }
        link(r.body.id, r.body.name).then(done);
      });
    } else { link(cur.id, cur.name).then(done); }
  };

  // ------------------------------------------------------- actualizaciones
  function checkUpdate() {
    api("GET", "/api/update").then(function (r) {
      if (!r.ok || !r.body.available) { return; }
      $("update-commits").innerHTML = r.body.commits.map(function (c) {
        var i = c.indexOf(" ");
        return "<li><code>" + esc(c.slice(0, i)) + "</code>" + esc(c.slice(i + 1)) + "</li>";
      }).join("");
      $("dlg-update").showModal();
    });
  }
  $("update-later").onclick = function () { $("dlg-update").close(); };
  $("update-go").onclick = function () {
    var go = $("update-go"), log = $("update-log");
    if (go.dataset.done) { go.disabled = true; api("POST", "/api/restart"); return; }  // un doble clic abría dos ventanas
    go.disabled = true; $("update-later").disabled = true; log.hidden = false; log.textContent = "Empezando…";
    api("POST", "/api/update").then(function (r) {
      if (!r.ok) { log.textContent = r.body.error || "No se pudo empezar."; go.disabled = false; $("update-later").disabled = false; return; }
      var poll = setInterval(function () {
        api("GET", "/api/update/log").then(function (l) {
          if (!l.ok) { return; }
          log.textContent = l.body.log; log.scrollTop = log.scrollHeight;
          if (l.body.state === "running") { return; }
          clearInterval(poll);
          $("update-later").disabled = false;
          go.disabled = false;
          if (l.body.state === "done") {
            go.dataset.done = "1"; go.textContent = "Reiniciar Drive";
            $("update-hint").textContent = "Actualizado. Reinicia la ventana para usar la versión nueva (la sincronización ya corre con ella).";
          } else {
            go.textContent = "Reintentar";
            $("update-hint").textContent = "No se pudo actualizar. Abajo está el motivo.";
          }
        });
      }, 1500);
    });
  };

  api("POST", "/api/chrome");
  checkUpdate();
  refresh(); refreshEvents();
  setInterval(refresh, 2000); setInterval(refreshEvents, 4000);
})();
