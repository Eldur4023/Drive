// Pequeñas mejoras de interfaz. La aplicación funciona sin JavaScript:
// todo lo que hay aquí es opcional.

(function () {
  "use strict";

  // Arrastrar y soltar sobre la zona de subida.
  var zone = document.querySelector("[data-dropzone]");
  var input = document.querySelector("[data-dropzone] input[type=file]");
  if (zone && input) {
    ["dragenter", "dragover"].forEach(function (type) {
      zone.addEventListener(type, function (event) {
        event.preventDefault();
        zone.classList.add("hover");
      });
    });
    ["dragleave", "drop"].forEach(function (type) {
      zone.addEventListener(type, function () {
        zone.classList.remove("hover");
      });
    });
    zone.addEventListener("drop", function (event) {
      event.preventDefault();
      input.files = event.dataTransfer.files;
      zone.closest("form").submit();
    });
    input.addEventListener("change", function () {
      if (input.files.length) {
        input.closest("form").submit();
      }
    });
  }

  // Confirmación en las acciones destructivas.
  document.querySelectorAll("[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) {
        event.preventDefault();
      }
    });
  });

  // Copiar al portapapeles (enlaces compartidos, tokens de API).
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var text = button.getAttribute("data-copy");
      navigator.clipboard.writeText(text).then(function () {
        var original = button.textContent;
        button.textContent = "Copiado";
        setTimeout(function () {
          button.textContent = original;
        }, 1500);
      });
    });
  });

  // Marcar/desmarcar todas las filas de una tabla con selección.
  var toggle = document.querySelector("[data-check-all]");
  if (toggle) {
    toggle.addEventListener("change", function () {
      document.querySelectorAll("input[name=selected]").forEach(function (box) {
        box.checked = toggle.checked;
      });
    });
  }
})();
