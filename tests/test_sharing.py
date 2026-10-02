"""Compartición entre usuarios, ficheros públicos y enlaces con token."""

from __future__ import annotations

from conftest import login, logout, register, upload


def test_lo_ajeno_no_se_ve(client):
    register(client, "ana")
    login(client, "ana")
    node = upload(client, "privado.txt", b"secreto")
    logout(client)

    register(client, "bruno")
    login(client, "bruno")
    # 404 y no 403: la existencia del fichero tampoco debe confirmarse.
    assert client.get(f"/api/files/{node['id']}").status_code == 404


def test_compartir_con_un_usuario(client):
    register(client, "ana")
    login(client, "ana")
    node = upload(client, "informe.txt", b"datos")
    logout(client)

    register(client, "bruno")
    logout(client)

    login(client, "ana")
    response = client.post(
        f"/api/files/{node['id']}/shares", data={"username": "bruno", "permission": "read"}
    )
    assert response.status_code == 201
    logout(client)

    login(client, "bruno")
    assert client.get(f"/api/files/{node['id']}").json()["permission"] == "read"
    assert client.get(f"/files/{node['id']}/download").content == b"datos"
    assert [n["id"] for n in client.get("/api/shared").json()["items"]] == [node["id"]]


def test_permiso_de_lectura_no_permite_escribir(client):
    register(client, "ana")
    login(client, "ana")
    node = upload(client, "informe.txt", b"datos")
    logout(client)
    register(client, "bruno")
    logout(client)

    login(client, "ana")
    client.post(f"/api/files/{node['id']}/shares", data={"username": "bruno", "permission": "read"})
    logout(client)

    login(client, "bruno")
    assert client.delete(f"/api/files/{node['id']}").status_code == 403


def test_comparticion_heredada_por_la_carpeta(client):
    register(client, "ana")
    login(client, "ana")
    carpeta = client.post("/api/folders", data={"name": "equipo"}).json()
    dentro = upload(client, "acta.txt", b"acta", parent=carpeta["id"])
    logout(client)
    register(client, "bruno")
    logout(client)

    login(client, "ana")
    client.post(f"/api/files/{carpeta['id']}/shares", data={"username": "bruno", "permission": "write"})
    logout(client)

    login(client, "bruno")
    detalle = client.get(f"/api/files/{dentro['id']}").json()
    assert detalle["permission"] == "write"
    assert detalle["access_via"] == "share"


def test_fichero_publico_sin_cuenta(client):
    register(client, "ana")
    login(client, "ana")
    node = upload(client, "folleto.pdf", b"contenido publico")
    client.patch(f"/api/files/{node['id']}", data={"is_public": "true"})
    logout(client)

    assert client.get(f"/p/{node['id']}").status_code == 200
    assert client.get(f"/p/{node['id']}/download").content == b"contenido publico"
    assert node["name"] in client.get("/browse").text


def test_dejar_de_ser_publico(client):
    register(client, "ana")
    login(client, "ana")
    node = upload(client, "folleto.pdf", b"x")
    client.patch(f"/api/files/{node['id']}", data={"is_public": "true"})
    client.patch(f"/api/files/{node['id']}", data={"is_public": "false"})
    logout(client)

    assert client.get(f"/p/{node['id']}").status_code == 404


def test_enlace_con_token(client):
    register(client, "ana")
    login(client, "ana")
    node = upload(client, "compartido.txt", b"por enlace")
    link = client.post(f"/api/files/{node['id']}/links", data={"mode": "download"}).json()
    logout(client)

    token = link["url"].rsplit("/", 1)[-1]
    assert client.get(f"/s/{token}").status_code == 200
    assert client.get(f"/s/{token}/download/{node['id']}").content == b"por enlace"


def test_enlace_con_password(client):
    register(client, "ana")
    login(client, "ana")
    node = upload(client, "reservado.txt", b"protegido")
    link = client.post(
        f"/api/files/{node['id']}/links", data={"mode": "download", "password": "abreteSesamo"}
    ).json()
    logout(client)
    token = link["url"].rsplit("/", 1)[-1]

    assert "contraseña" in client.get(f"/s/{token}").text.lower()
    assert client.post(f"/s/{token}", data={"password": "otra"}, follow_redirects=False).status_code == 401

    ok = client.post(f"/s/{token}", data={"password": "abreteSesamo"}, follow_redirects=False)
    assert ok.status_code == 303
    assert client.get(f"/s/{token}/download/{node['id']}").content == b"protegido"


def test_enlace_revocado(client):
    register(client, "ana")
    login(client, "ana")
    node = upload(client, "temporal.txt", b"x")
    link = client.post(f"/api/files/{node['id']}/links", data={}).json()
    token = link["url"].rsplit("/", 1)[-1]

    client.post(f"/links/{link['id']}/revoke", follow_redirects=False)
    logout(client)
    assert client.get(f"/s/{token}").status_code == 410


def test_limite_de_descargas_del_enlace(client):
    register(client, "ana")
    login(client, "ana")
    node = upload(client, "limitado.txt", b"x")
    link = client.post(
        f"/api/files/{node['id']}/links", data={"download_limit": 1}
    ).json()
    logout(client)
    token = link["url"].rsplit("/", 1)[-1]

    assert client.get(f"/s/{token}/download/{node['id']}").status_code == 200
    assert client.get(f"/s/{token}/download/{node['id']}").status_code == 410


def test_buzon_de_subida(client):
    register(client, "ana")
    login(client, "ana")
    carpeta = client.post("/api/folders", data={"name": "buzon"}).json()
    link = client.post(
        f"/api/files/{carpeta['id']}/links", data={"mode": "upload"}
    ).json()
    logout(client)
    token = link["url"].rsplit("/", 1)[-1]

    response = client.post(
        f"/s/{token}/upload",
        data={"node_id": carpeta["id"]},
        files={"files": ("de-fuera.txt", b"aportacion externa", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    login(client, "ana")
    dentro = client.get("/api/files", params={"parent_id": carpeta["id"]}).json()["items"]
    assert [item["name"] for item in dentro] == ["de-fuera.txt"]


def test_enlaces_publicos_desactivados(make_app):
    with make_app({"sharing": {"public_links_enabled": False}}) as client:
        register(client, "ana")
        login(client, "ana")
        node = upload(client, "x.txt", b"x")
        response = client.post(f"/api/files/{node['id']}/links", data={})
        assert response.status_code == 403


def test_menu_contextual_comparte_con_la_sesion_web(client):
    # El clic derecho de la web llama a la API con la cookie de sesión.
    register(client, "bob")
    register(client, "ana")
    login(client, "ana")
    node = upload(client, "nota.txt", b"hola")

    enlace = client.post(f"/api/files/{node['id']}/links", data={"mode": "download"})
    assert enlace.status_code == 201, enlace.text
    assert enlace.json()["url"].startswith("http")

    share = client.post(f"/api/files/{node['id']}/shares", data={"username": "bob", "permission": "write"})
    assert share.status_code == 201, share.text
    assert share.json() == {"id": share.json()["id"], "user": "bob", "permission": "write"}

    nadie = client.post(f"/api/files/{node['id']}/shares", data={"username": "nadie"})
    assert nadie.status_code == 404 and nadie.json()["detail"] == "No existe ese usuario."

    pagina = client.get("/files")
    assert f'data-id="{node["id"]}"' in pagina.text and "data-can-share" in pagina.text
