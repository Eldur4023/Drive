// Mejoras de interfaz. La aplicación funciona sin JavaScript:
// todo lo que hay aquí es opcional y cae al comportamiento normal de los formularios.

(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) { /* sin almacenamiento */ } },
  };
  var icon = function (name) {
    return '<svg class="ico"><use href="#i-' + name + '"/></svg>';
  };
  var esc = function (s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var fmtSize = function (n) {
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  };

  // Cierra el menú de cuenta al pulsar fuera o con Escape.
  var menu = $("details.menu");
  if (menu) {
    document.addEventListener("click", function (e) { if (!menu.contains(e.target)) { menu.open = false; } });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") { menu.open = false; } });
  }

  // ------------------------------------------------------------ avisos
  var toasts = $("#toasts");
  function toast(text, ms, bad) {
    if (!toasts) { return; }
    var el = document.createElement("div");
    el.className = bad ? "toast bad" : "toast";
    el.innerHTML = icon(bad ? "x" : "check") + "<span>" + esc(text) + "</span>";
    toasts.appendChild(el);
    setTimeout(function () {
      el.classList.add("out");
      setTimeout(function () { el.remove(); }, 300);
    }, ms || 2200);
  }
  // Los avisos «ok» que llegan del servidor se muestran como toast y se retiran.
  $$(".notice.ok").forEach(function (n) {
    toast(n.textContent.trim(), 3200);
    n.remove();
  });

  // ------------------------------------------------ diálogo de confirmación
  var dlg = $("#confirm");
  function ask(text, okLabel) {
    return new Promise(function (resolve) {
      if (!dlg || typeof dlg.showModal !== "function") { resolve(window.confirm(text)); return; }
      $("#confirm-text").textContent = text;
      $("#confirm-ok").textContent = okLabel || "Confirmar";
      dlg.addEventListener("close", function onClose() {
        dlg.removeEventListener("close", onClose);
        resolve(dlg.returnValue === "ok");
      });
      dlg.showModal();
    });
  }
  $$("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (form.dataset.confirmed) { return; }
      event.preventDefault();
      var submitter = event.submitter;
      ask(form.getAttribute("data-confirm")).then(function (yes) {
        if (!yes) { return; }
        form.dataset.confirmed = "1";
        if (form.requestSubmit) { form.requestSubmit(submitter); } else { form.submit(); }
      });
    });
  });

  // ----------------------------------------------- copiar al portapapeles
  $$("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(button.getAttribute("data-copy")).then(function () {
        toast("Copiado al portapapeles");
      });
    });
  });

  // Marcar/desmarcar todas las filas.
  var toggle = $("[data-check-all]");
  if (toggle) {
    toggle.addEventListener("change", function () {
      $$("input[name=selected]").forEach(function (box) { box.checked = toggle.checked; });
    });
  }

  // Miniatura: oculta el icono del tipo mientras se muestra la imagen; si falla, vuelve al icono.
  $$("img.thumb").forEach(function (img) {
    var ico = img.nextElementSibling;
    if (ico) { ico.setAttribute("hidden", ""); }  // en SVG no existe la propiedad .hidden
    img.addEventListener("error", function () { img.hidden = true; if (ico) { ico.removeAttribute("hidden"); } });
  });

  // Selectores que envían su formulario al cambiar (sustituye a onchange=, que
  // la política CSP bloquea).
  $$("[data-autosubmit]").forEach(function (el) {
    el.addEventListener("change", function () { el.form.submit(); });
  });

  // -------------------------------------------------- vista lista/cuadrícula
  var files = $("[data-files]");
  if (files) {
    var buttons = $$("[data-view]");
    var setView = function (v) {
      files.classList.toggle("grid-view", v === "grid");
      buttons.forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.view === v)); });
      store.set("drive-view", v);
    };
    buttons.forEach(function (b) { b.addEventListener("click", function () { setView(b.dataset.view); }); });
    setView(store.get("drive-view") === "grid" ? "grid" : "list");
  }

  // ----------------------------------------------------------- subidas
  // Cada zona con data-dropzone sube con XMLHttpRequest para mostrar progreso.
  // Sin JavaScript (o si algo falla antes de empezar) se envía el formulario normal.
  var queue = null;
  function ensureQueue() {
    if (!queue) {
      queue = document.createElement("div");
      queue.className = "uploads";
      document.body.appendChild(queue);
    }
    return queue;
  }

  // Normaliza a [{file, path}]; path es la ruta relativa dentro de la carpeta subida.
  function toEntries(fileList) {
    return Array.prototype.map.call(fileList, function (f) {
      return { file: f, path: f.webkitRelativePath || f.name };
    });
  }

  // Recorre carpetas arrastradas (webkitGetAsEntry) y devuelve todos sus ficheros.
  function collect(dt) {
    var roots = [];
    Array.prototype.forEach.call(dt.items || [], function (it) {
      var entry = it.kind === "file" && it.webkitGetAsEntry ? it.webkitGetAsEntry() : null;
      if (entry) { roots.push(entry); }
    });
    if (!roots.length) { return Promise.resolve(toEntries(dt.files)); }

    var readAll = function (reader) {  // readEntries devuelve a trozos hasta quedar vacío
      return new Promise(function (resolve, reject) {
        var all = [];
        (function more() {
          reader.readEntries(function (batch) {
            if (!batch.length) { resolve(all); return; }
            all = all.concat(Array.prototype.slice.call(batch));
            more();
          }, reject);
        })();
      });
    };
    var walk = function (entry, prefix) {
      if (entry.isFile) {
        return new Promise(function (resolve, reject) { entry.file(resolve, reject); })
          .then(function (f) { return [{ file: f, path: prefix + entry.name }]; });
      }
      return readAll(entry.createReader()).then(function (kids) {
        return Promise.all(kids.map(function (k) { return walk(k, prefix + entry.name + "/"); }));
      }).then(function (parts) { return [].concat.apply([], parts); });
    };
    return Promise.all(roots.map(function (r) { return walk(r, ""); }))
      .then(function (parts) { return [].concat.apply([], parts); });
  }

  // Una sola cola para toda la página: se pueden ir añadiendo carpetas (el
  // selector del sistema sólo deja elegir una cada vez) y se suben de una en una.
  // Una a una, no en paralelo: dos subidas con el mismo contenido a la vez
  // chocan al deduplicar el blob en el servidor.
  var up = { jobs: [], total: 0, done: 0, failed: 0, bytes: 0, sent: 0, busy: false, timer: null, url: null, card: null, firstError: "" };

  function paintCard(current) {
    var c = up.card;
    if (!c) { return; }
    var finished = !up.busy && !up.jobs.length;
    var pct = up.bytes ? Math.min(100, Math.round((up.sent / up.bytes) * 100)) : 100;
    c.classList.toggle("done", finished && !up.failed);
    c.classList.toggle("fail", finished && up.failed > 0);
    var title = finished
      ? (up.failed ? up.failed + " de " + up.total + " con error" : up.total + (up.total === 1 ? " fichero subido" : " ficheros subidos"))
      : "Subiendo " + Math.min(up.done + up.failed + 1, up.total) + " de " + up.total;
    c.innerHTML =
      '<div class="u-top">' + icon(finished ? (up.failed ? "x" : "check") : "upload") +
      '<span class="u-name">' + esc(title) + '</span><span class="u-pct">' + (finished ? fmtSize(up.bytes) : pct + "%") + '</span></div>' +
      '<div class="meter"><span style="width:' + pct + '%"></span></div>' +
      '<div class="muted u-sub">' + esc(finished ? up.firstError : (current || "")) + '</div>' +
      (finished && up.failed ? '<button type="button" class="small" data-reload style="margin-top:8px">Actualizar</button>' : "");
    var btn = $("[data-reload]", c);
    if (btn) { btn.addEventListener("click", function () { window.location.href = up.url || window.location.href; }); }
  }

  function pump() {
    var job = up.jobs.shift();
    if (!job) {
      up.busy = false;
      paintCard();
      // Todo bien: recarga la carpeta con el aviso del servidor. Con un margen, por si se añade otra carpeta.
      if (!up.failed) { up.timer = setTimeout(function () { window.location.href = up.url || window.location.href; }, 2500); }
      return;
    }
    up.busy = true;
    paintCard(job.path);

    var data = new FormData();
    $$("input[type=hidden], input[type=checkbox]:checked", job.form).forEach(function (f) { data.append(f.name, f.value); });
    if (job.path.indexOf("/") >= 0) { data.append("relpath", job.path); }
    data.append("files", job.file, job.file.name);

    var fail = function (msg) {
      up.failed++;
      if (!up.firstError) { up.firstError = job.path + ": " + msg; }
      up.sent += job.file.size;
      pump();
    };
    var xhr = new XMLHttpRequest();
    xhr.open("POST", job.form.getAttribute("action"));
    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable) { return; }
      var keep = up.sent;
      up.sent = keep + e.loaded;
      paintCard(job.path);
      up.sent = keep;
    };
    xhr.onload = function () {
      if (xhr.status < 200 || xhr.status >= 400) { fail("error " + xhr.status); return; }
      // Los errores de negocio (cuota, nombre no válido…) llegan en la redirección.
      var err = new URL(xhr.responseURL).searchParams.get("error");
      if (err) { fail(err); return; }
      up.url = xhr.responseURL;
      up.done++;
      up.sent += job.file.size;
      pump();
    };
    xhr.onerror = function () { fail("error de red"); };
    xhr.send(data);
  }

  function uploadForm(form, input) {
    var list = Array.isArray(input) ? input : toEntries(input);
    if (!list.length) { return; }
    clearTimeout(up.timer);
    if (!up.busy && !up.jobs.length) {  // lote nuevo: contadores a cero y tarjeta nueva
      if (up.card) { up.card.remove(); }
      up.total = up.done = up.failed = up.bytes = up.sent = 0;
      up.firstError = "";
      up.card = document.createElement("div");
      up.card.className = "upload-item";
      ensureQueue().appendChild(up.card);
    }
    list.forEach(function (e) {
      up.jobs.push({ form: form, file: e.file, path: e.path });
      up.total++;
      up.bytes += e.file.size;
    });
    if (!up.busy) { pump(); } else { paintCard(); }
  }

  $$("[data-dropzone]").forEach(function (zone) {
    var input = $("input[type=file]", zone);
    var form = zone.closest("form");
    if (!input || !form) { return; }
    var label = $(".picked", zone);

    // Zona principal: arrastrar/pulsar sube de inmediato; el botón «Subir» sobra.
    var auto = zone.hasAttribute("data-auto");
    var go = function (fl) {
      if (auto) { uploadForm(form, fl); }
      else if (label) {
        label.textContent = fl.length === 1 ? fl[0].name : fl.length + " ficheros seleccionados";
      }
    };

    ["dragenter", "dragover"].forEach(function (t) {
      zone.addEventListener(t, function (e) { e.preventDefault(); zone.classList.add("hover"); });
    });
    ["dragleave", "drop"].forEach(function (t) {
      zone.addEventListener(t, function () { zone.classList.remove("hover"); });
    });
    zone.addEventListener("drop", function (e) {
      e.preventDefault();
      if (auto) { collect(e.dataTransfer).then(function (list) { uploadForm(form, list); }); return; }
      if (!e.dataTransfer.files.length) { return; }
      input.files = e.dataTransfer.files;
      go(input.files);
    });
    input.addEventListener("change", function () { if (input.files.length) { go(input.files); } });

    // Arrastrar sobre cualquier punto de la página sube a la zona principal.
    if (auto) {
      var overlay = document.createElement("div");
      overlay.className = "drop-overlay";
      overlay.innerHTML = "<div>" + icon("upload") + "Suelta para subir</div>";
      document.body.appendChild(overlay);
      var depth = 0;
      var hasFiles = function (e) { return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types, "Files") >= 0; };
      window.addEventListener("dragenter", function (e) { if (hasFiles(e)) { depth++; overlay.classList.add("on"); } });
      window.addEventListener("dragleave", function (e) { if (hasFiles(e) && --depth <= 0) { depth = 0; overlay.classList.remove("on"); } });
      window.addEventListener("dragover", function (e) { if (hasFiles(e)) { e.preventDefault(); } });
      window.addEventListener("drop", function (e) {
        if (!hasFiles(e)) { return; }
        e.preventDefault(); depth = 0; overlay.classList.remove("on");
        if (!zone.contains(e.target)) { collect(e.dataTransfer).then(function (list) { uploadForm(form, list); }); }
      });
    }
  });

  // Botón «Subir» de la barra: abre el selector de la zona principal.
  $$("[data-pick]").forEach(function (b) {
    b.addEventListener("click", function () {
      var input = $("[data-dropzone] input[type=file]");
      if (input) { input.click(); }
    });
  });

  // Botón «Subir carpeta»: selector de directorios; conserva la estructura.
  $$("[data-pick-dir]").forEach(function (b) {
    b.addEventListener("click", function () {
      var zone = $("[data-dropzone][data-auto]");
      if (!zone) { return; }
      var picker = document.createElement("input");
      picker.type = "file";
      picker.webkitdirectory = true;
      picker.multiple = true;
      picker.addEventListener("change", function () {
        if (picker.files.length) { uploadForm(zone.closest("form"), picker.files); }
      });
      picker.click();
    });
  });

  // «Nueva carpeta» como diálogo con campo de texto.
  $$("[data-prompt-form]").forEach(function (form) {
    var trigger = $("[data-prompt-open]", form.parentNode) || $("[data-prompt-open]");
    if (!trigger || !dlg || typeof dlg.showModal !== "function") { return; }
    var field = $("input[name=name]", form);
    trigger.addEventListener("click", function () {
      var box = document.createElement("dialog");
      box.className = "modal prompt";
      box.innerHTML =
        '<form method="dialog"><div class="modal-icon">' + icon("folder-plus") + '</div>' +
        '<h3>Nueva carpeta</h3><input type="text" placeholder="Nombre de la carpeta" maxlength="255" required>' +
        '<div class="modal-actions"><button value="cancel" class="button" formnovalidate>Cancelar</button>' +
        '<button value="ok" class="button primary">Crear</button></div></form>';
      document.body.appendChild(box);
      var input = $("input", box);
      box.addEventListener("close", function () {
        if (box.returnValue === "ok" && input.value.trim()) {
          field.value = input.value.trim();
          form.submit();
        }
        box.remove();
      });
      box.showModal();
      input.focus();
    });
  });

  // ------------------------------------------------ menú del clic derecho
  // La página se queda el clic derecho: sobre un fichero o carpeta abre este
  // menú y en el resto no hace nada. En los campos de texto se deja el del
  // navegador para poder pegar.
  var ctx = $("#ctx"), ctxRow = null;
  // Colgado de <body>: <main> se anima con transform y eso desplazaría el position: fixed.
  if (ctx) { document.body.appendChild(ctx); }
  function closeCtx() {
    if (ctx) { ctx.hidden = true; }
    if (ctxRow) { ctxRow.classList.remove("sel"); ctxRow = null; }
  }
  // La API responde {detail} en los errores.
  function post(url, fields) {
    return fetch(url, { method: "POST", body: new URLSearchParams(fields), headers: { Accept: "application/json" } })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) { throw new Error(j.detail || "Error " + r.status); }
          return j;
        });
      });
  }
  function copyLink(url) {
    var shown = function () { window.prompt("Enlace para compartir:", url); };
    if (!navigator.clipboard) { shown(); return; }  // sólo existe con https o localhost
    navigator.clipboard.writeText(url).then(function () { toast("Enlace copiado al portapapeles", 3200); }, shown);
  }
  var ctxActions = {
    open: function (row) { location.href = "/files/" + row.dataset.id; },
    download: function (row) { location.href = "/files/" + row.dataset.id + "/download"; },
    detail: function (row) { location.href = "/files/" + row.dataset.id + "/detail"; },
    link: function (row) {
      post("/api/files/" + row.dataset.id + "/links", { mode: "download" })
        .then(function (j) { copyLink(j.url); }, function (e) { toast(e.message, 4000, true); });
    },
    share: function (row) {
      var box = $("#share-dlg"), user = $("#share-user");
      $("#share-title").textContent = "Compartir «" + row.dataset.name + "»";
      user.value = "";
      box.addEventListener("close", function onClose() {
        box.removeEventListener("close", onClose);
        if (box.returnValue !== "ok" || !user.value.trim()) { return; }
        post("/api/files/" + row.dataset.id + "/shares", { username: user.value.trim(), permission: $("#share-perm").value })
          .then(function (j) { toast("Compartido con " + j.user, 3200); }, function (e) { toast(e.message, 4000, true); });
      });
      box.showModal();
      user.focus();
    },
  };
  document.addEventListener("contextmenu", function (e) {
    if (e.target.closest("input, textarea, [contenteditable]")) { return; }
    e.preventDefault();
    closeCtx();
    var row = e.target.closest("[data-ctx] tr[data-id]");
    if (!row || !ctx) { return; }
    var body = row.parentNode, dir = row.hasAttribute("data-dir");
    var items = [dir ? ["open", "folder", "Abrir"] : ["download", "download", "Descargar"]];
    if (body.hasAttribute("data-can-link") || body.hasAttribute("data-can-share")) { items.push(null); }
    if (body.hasAttribute("data-can-link")) { items.push(["link", "link", "Crear enlace para compartir"]); }
    if (body.hasAttribute("data-can-share")) { items.push(["share", "users", "Compartir con un usuario…"]); }
    items.push(null, ["detail", "info", "Detalles"]);
    ctx.innerHTML = items.map(function (it) {
      return it ? '<button type="button" role="menuitem" data-act="' + it[0] + '">' + icon(it[1]) + esc(it[2]) + "</button>" : "<hr>";
    }).join("");
    ctxRow = row;
    row.classList.add("sel");
    ctx.hidden = false;
    ctx.style.left = Math.min(e.clientX, window.innerWidth - ctx.offsetWidth - 8) + "px";
    ctx.style.top = Math.min(e.clientY, window.innerHeight - ctx.offsetHeight - 8) + "px";
    ctx.querySelector("button").focus();
  });
  if (ctx) {
    ctx.addEventListener("click", function (e) {
      var b = e.target.closest("button[data-act]"), row = ctxRow;
      if (!b) { return; }
      closeCtx();
      ctxActions[b.dataset.act](row);
    });
    document.addEventListener("click", function (e) { if (!ctx.contains(e.target)) { closeCtx(); } });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") { closeCtx(); } });
    window.addEventListener("scroll", closeCtx, true);
    window.addEventListener("resize", closeCtx);
    window.addEventListener("blur", closeCtx);
  }
})();
