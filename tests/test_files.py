"""Subida, descarga, cuota, papelera y versiones."""

from __future__ import annotations

from conftest import login, register, upload


def test_subir_y_descargar(client):
    register(client, "ana")
    login(client, "ana")

    node = upload(client, "notas.txt", b"hola mundo")
    assert node["size"] == 10

    response = client.get(f"/files/{node['id']}/download")
    assert response.status_code == 200
    assert response.content == b"hola mundo"
    assert "notas.txt" in response.headers["content-disposition"]


def test_carpetas_y_jerarquia(client):
    register(client, "ana")
    login(client, "ana")

    folder = client.post("/api/folders", data={"name": "Documentos"}).json()
    node = upload(client, "dentro.txt", b"x", parent=folder["id"])

    listing = client.get("/api/files", params={"parent_id": folder["id"]}).json()
    assert [item["id"] for item in listing["items"]] == [node["id"]]

    detail = client.get(f"/api/files/{node['id']}").json()
    assert [step["name"] for step in detail["path"]] == ["Documentos", "dentro.txt"]


def test_tamano_de_carpetas(client):
    register(client, "ana")
    login(client, "ana")

    docs = client.post("/api/folders", data={"name": "Documentos"}).json()
    sub = client.post("/api/folders", data={"name": "Sub", "parent_id": docs["id"]}).json()
    upload(client, "a.txt", b"x" * 10, parent=docs["id"])
    borrado = upload(client, "b.txt", b"x" * 5, parent=sub["id"])
    upload(client, "c.txt", b"x" * 7, parent=sub["id"])
    client.delete(f"/api/files/{borrado['id']}")  # lo de la papelera no cuenta

    sizes = {i["name"]: i["size"] for i in client.get("/api/files").json()["items"]}
    assert sizes["Documentos"] == 17
    sizes = {i["name"]: i["size"] for i in client.get("/api/files", params={"parent_id": docs["id"]}).json()["items"]}
    assert sizes["Sub"] == 7
    assert '<td class="hide-sm muted">17 B</td>' in client.get("/files").text


def test_nombres_duplicados_se_desambiguan(client):
    register(client, "ana")
    login(client, "ana")

    primero = upload(client, "informe.txt", b"uno")
    segundo = upload(client, "informe.txt", b"dos")
    assert primero["name"] == "informe.txt"
    assert segundo["name"] == "informe (2).txt"


def test_deduplicacion_comparte_el_blob(client, tmp_path):
    register(client, "ana")
    login(client, "ana")

    upload(client, "a.txt", b"contenido identico")
    upload(client, "b.txt", b"contenido identico")

    blobs = list((tmp_path / "blobs").rglob("*"))
    ficheros = [p for p in blobs if p.is_file() and p.parent.name != "tmp"]
    assert len(ficheros) == 1, "el mismo contenido debe guardarse una sola vez"


def test_cuota_rechaza_lo_que_no_cabe(make_app):
    with make_app({"storage": {"default_quota": "1KB"}, "roles": {"user": {"quota": "1KB"}}}) as client:
        register(client, "jefe")
        register(client, "limitado")
        login(client, "limitado")

        response = client.post(
            "/api/files", files={"file": ("grande.bin", b"x" * 2048, "application/octet-stream")}
        )
        assert response.status_code == 400
        assert "espacio" in response.json()["detail"].lower()


def test_tamano_maximo_por_fichero(make_app):
    with make_app({"storage": {"max_upload_size": "10B"}, "roles": {"admin": {"max_upload_size": "10B"}}}) as client:
        register(client, "ana")
        login(client, "ana")
        response = client.post("/api/files", files={"file": ("x.bin", b"y" * 100, "text/plain")})
        assert response.status_code == 400
        assert "máximo" in response.json()["detail"]


def test_extension_bloqueada(make_app):
    with make_app({"storage": {"blocked_extensions": [".exe"]}}) as client:
        register(client, "ana")
        login(client, "ana")
        response = client.post("/api/files", files={"file": ("virus.exe", b"MZ", "application/x-msdownload")})
        assert response.status_code == 400
        assert "exe" in response.json()["detail"]


def test_papelera_y_restauracion(client):
    register(client, "ana")
    login(client, "ana")
    node = upload(client, "borrable.txt", b"adios")

    client.delete(f"/api/files/{node['id']}")
    assert client.get("/api/files").json()["items"] == []
    assert [n["id"] for n in client.get("/api/trash").json()["items"]] == [node["id"]]

    client.post(f"/api/files/{node['id']}/restore")
    assert [n["id"] for n in client.get("/api/files").json()["items"]] == [node["id"]]


def test_borrado_definitivo_libera_espacio(client):
    register(client, "ana")
    login(client, "ana")
    node = upload(client, "grande.txt", b"z" * 500)
    assert client.get("/api/usage").json()["used"] == 500

    client.delete(f"/api/files/{node['id']}", params={"permanent": True})
    assert client.get("/api/usage").json()["used"] == 0


def test_versiones_al_sobrescribir(client):
    register(client, "ana")
    login(client, "ana")

    primero = upload(client, "doc.txt", b"version uno")
    response = client.post(
        "/api/files",
        data={"overwrite": "true"},
        files={"file": ("doc.txt", b"version dos", "text/plain")},
    )
    assert response.status_code == 201
    assert response.json()["id"] == primero["id"], "debe reutilizar el mismo nodo"

    contenido = client.get(f"/files/{primero['id']}/download").content
    assert contenido == b"version dos"

    detalle = client.get(f"/files/{primero['id']}/detail")
    assert "Versiones anteriores" in detalle.text


def test_rango_para_streaming(client):
    register(client, "ana")
    login(client, "ana")
    node = upload(client, "video.bin", b"0123456789")

    response = client.get(f"/files/{node['id']}/download", headers={"Range": "bytes=2-5"})
    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"


def test_no_se_puede_mover_una_carpeta_dentro_de_si_misma(client):
    register(client, "ana")
    login(client, "ana")
    padre = client.post("/api/folders", data={"name": "padre"}).json()
    hijo = client.post("/api/folders", data={"name": "hijo", "parent_id": padre["id"]}).json()

    response = client.patch(f"/api/files/{padre['id']}", data={"parent_id": hijo["id"]})
    assert response.status_code == 400


def test_nombre_con_separadores_se_sanea(client):
    register(client, "ana")
    login(client, "ana")
    node = upload(client, "../../etc/passwd", b"raiz")
    assert "/" not in node["name"] and ".." not in node["name"]


def test_subidas_simultaneas_con_el_mismo_contenido(client):
    """Antes chocaban con la clave única de blobs.hash al deduplicar (500)."""
    from concurrent.futures import ThreadPoolExecutor

    register(client, "ana")
    login(client, "ana")

    def subir(i):
        return client.post(
            "/api/files",
            files={"file": (f"copia{i}.txt", b"mismo contenido", "text/plain")},
        ).status_code

    with ThreadPoolExecutor(8) as pool:
        codigos = list(pool.map(subir, range(8)))
    assert codigos == [201] * 8


def test_subir_carpeta_crea_la_estructura(client):
    register(client, "ana")
    login(client, "ana")

    def subir(nombre, ruta, contenido):
        r = client.post(
            "/api/files",
            data={"relpath": ruta},
            files={"file": (nombre, contenido, "text/plain")},
        )
        assert r.status_code == 201, r.text
        return r.json()

    a = subir("a.txt", "fotos/2024/a.txt", b"a")
    subir("b.txt", "fotos/2024/b.txt", b"b")      # reutiliza las carpetas
    subir("c.txt", "fotos/../../c.txt", b"c")     # se descartan los «..»: queda en fotos/

    raiz = client.get("/api/files").json()["items"]
    assert [i["name"] for i in raiz] == ["fotos"]
    fotos = next(i for i in raiz if i["name"] == "fotos")
    sub = client.get("/api/files", params={"parent_id": fotos["id"]}).json()["items"]
    assert sorted(i["name"] for i in sub) == ["2024", "c.txt"]
    anio = next(i for i in sub if i["name"] == "2024")
    hojas = client.get("/api/files", params={"parent_id": anio["id"]}).json()["items"]
    assert sorted(i["name"] for i in hojas) == ["a.txt", "b.txt"]
    assert a["name"] == "a.txt"


def test_arbol_de_sincronizacion(client):
    register(client, "ana")
    login(client, "ana")

    for ruta, dato in (("a/b/uno.txt", b"1"), ("a/dos.txt", b"22"), ("raiz.txt", b"333")):
        r = client.post("/api/files", data={"relpath": ruta}, files={"file": (ruta.split("/")[-1], dato, "text/plain")})
        assert r.status_code == 201, r.text

    arbol = client.get("/api/sync/tree").json()
    assert arbol["hash_algorithm"] == "sha256"
    por_ruta = {i["path"]: i for i in arbol["items"]}
    assert set(por_ruta) == {"a", "a/b", "a/b/uno.txt", "a/dos.txt", "raiz.txt"}
    assert por_ruta["a"]["is_dir"] and por_ruta["a"]["hash"] is None
    assert por_ruta["a/dos.txt"]["size"] == 2 and len(por_ruta["a/dos.txt"]["hash"]) == 64

    # subárbol: rutas relativas a la carpeta pedida
    sub = client.get("/api/sync/tree", params={"root_id": por_ruta["a"]["id"]}).json()
    assert {i["path"] for i in sub["items"]} == {"b", "b/uno.txt", "dos.txt"}

    # lo que va a la papelera desaparece del árbol
    client.delete(f"/api/files/{por_ruta['raiz.txt']['id']}")
    assert "raiz.txt" not in {i["path"] for i in client.get("/api/sync/tree").json()["items"]}
