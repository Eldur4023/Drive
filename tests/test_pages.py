"""Recorrido de todas las páginas HTML.

Las plantillas Jinja no se compilan hasta que se renderizan, así que un fallo
tonto en una de ellas no aparece en ningún otro test.
"""

from __future__ import annotations

import pytest

from conftest import login, logout, register, upload


@pytest.fixture
def poblada(client):
    """Instancia con contenido suficiente para que las vistas tengan qué pintar."""
    register(client, "jefa")
    login(client, "jefa")

    carpeta = client.post("/api/folders", data={"name": "Proyecto"}).json()
    fichero = upload(client, "memoria.txt", b"contenido", parent=carpeta["id"])
    borrado = upload(client, "descartado.txt", b"basura")
    client.delete(f"/api/files/{borrado['id']}")

    # Una segunda versión, un enlace y una compartición.
    client.post(
        "/api/files",
        data={"parent_id": carpeta["id"], "overwrite": "true"},
        files={"file": ("memoria.txt", b"contenido revisado", "text/plain")},
    )
    link = client.post(f"/api/files/{fichero['id']}/links", data={}).json()
    register(client, "colega")
    login(client, "jefa")
    client.post(f"/api/files/{fichero['id']}/shares", data={"username": "colega"})
    client.patch(f"/api/files/{carpeta['id']}", data={"is_public": "true"})

    return {"client": client, "carpeta": carpeta, "fichero": fichero, "link": link}


@pytest.mark.parametrize(
    "ruta",
    [
        "/files",
        "/shared",
        "/trash",
        "/links",
        "/profile",
        "/search?q=memoria",
        "/browse",
        "/admin",
        "/admin/users",
        "/admin/settings",
        "/admin/audit",
        "/2fa/setup",
    ],
)
def test_paginas_de_usuario_identificado(poblada, ruta):
    response = poblada["client"].get(ruta)
    assert response.status_code == 200, response.text[:400]
    assert "<html" in response.text


def test_paginas_de_un_nodo(poblada):
    client = poblada["client"]
    for ruta in (
        f"/files/{poblada['carpeta']['id']}",
        f"/files/{poblada['fichero']['id']}/detail",
        f"/files/{poblada['carpeta']['id']}/detail",
    ):
        assert client.get(ruta).status_code == 200, ruta


def test_paginas_sin_cuenta(poblada):
    client = poblada["client"]
    token = poblada["link"]["url"].rsplit("/", 1)[-1]
    logout(client)

    for ruta in ("/login", "/register", "/browse", f"/s/{token}"):
        response = client.get(ruta)
        assert response.status_code == 200, f"{ruta}: {response.text[:300]}"

    assert client.get(f"/browse?node_id={poblada['carpeta']['id']}").status_code == 200
    # Lo que cuelga de una carpeta pública hereda su visibilidad.
    assert client.get(f"/p/{poblada['fichero']['id']}").status_code == 200


def test_pagina_de_error(client):
    response = client.get("/files/noexiste/detail", follow_redirects=False)
    assert response.status_code in (303, 404)


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_mantenimiento_limpia_blobs_huerfanos(client):
    from app import maintenance
    from app.models import Blob
    from app.database import session_scope

    register(client, "ana")
    login(client, "ana")
    node = upload(client, "efimero.txt", b"contenido efimero")
    client.delete(f"/api/files/{node['id']}", params={"permanent": True})

    with session_scope() as db:
        assert db.query(Blob).count() == 1, "la fila queda hasta el mantenimiento"

    resultado = maintenance.run_once()
    assert resultado["blobs"] == 1

    with session_scope() as db:
        assert db.query(Blob).count() == 0
