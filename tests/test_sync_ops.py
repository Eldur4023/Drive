"""POST /api/sync/ops: operaciones de sincronización por lotes."""

from conftest import login, logout, register, upload


def ops(client, *lista, run="t1"):
    response = client.post("/api/sync/ops", json={"run": run, "ops": list(lista)})
    assert response.status_code == 200, response.text
    return response.json()["results"]


def tree(client):
    return {i["path"]: i["id"] for i in client.get("/api/sync/tree").json()["items"]}


def test_mkdir_con_referencias_dentro_del_lote(client):
    register(client, "ana")
    login(client, "ana")
    raiz = client.post("/api/folders", data={"name": "raiz"}).json()["id"]

    res = ops(
        client,
        {"op": "mkdir", "key": "a", "parent": raiz, "name": "a", "ref": "a"},
        {"op": "mkdir", "key": "b", "parent": "$a", "name": "b", "ref": "a/b"},
        {"op": "mkdir", "key": "c", "parent": "$a/b", "name": "c"},
    )
    assert [r["ok"] for r in res] == [True, True, True]
    assert {"raiz/a", "raiz/a/b", "raiz/a/b/c"} <= set(tree(client))


def test_mkdir_es_idempotente_y_no_duplica(client):
    register(client, "ana")
    login(client, "ana")
    raiz = client.post("/api/folders", data={"name": "raiz"}).json()["id"]
    op = {"op": "mkdir", "key": "x", "parent": raiz, "name": "x"}
    primero = ops(client, op)[0]
    segundo = ops(client, op)[0]
    assert primero["ok"] and not primero["unchanged"]
    assert segundo["ok"] and segundo["unchanged"] and segundo["id"] == primero["id"]


def test_mkdir_no_pisa_un_fichero_del_mismo_nombre(client):
    register(client, "ana")
    login(client, "ana")
    raiz = client.post("/api/folders", data={"name": "raiz"}).json()["id"]
    upload(client, "x", b"datos", parent=raiz)
    res = ops(client, {"op": "mkdir", "key": "x", "parent": raiz, "name": "x"})[0]
    assert not res["ok"] and "fichero" in res["error"]


def test_mover_y_renombrar_conservan_el_nodo_y_el_nombre(client):
    register(client, "ana")
    login(client, "ana")
    destino = client.post("/api/folders", data={"name": "destino"}).json()["id"]
    nodo = upload(client, "a.txt", b"hola")

    res = ops(client, {"op": "move", "key": "m", "id": nodo["id"], "parent": destino, "name": "b.txt"})[0]
    assert res["ok"] and res["id"] == nodo["id"] and not res["unchanged"]
    movido = client.get(f"/api/files/{nodo['id']}").json()
    assert movido["name"] == "b.txt" and movido["parent_id"] == destino

    repetido = ops(client, {"op": "move", "key": "m", "id": nodo["id"], "parent": destino, "name": "b.txt"})[0]
    assert repetido["ok"] and repetido["unchanged"]


def test_mover_a_un_nombre_ocupado_falla_sin_renombrar_a_la_fuerza(client):
    register(client, "ana")
    login(client, "ana")
    a = upload(client, "a.txt", b"1")
    upload(client, "b.txt", b"2")
    res = ops(client, {"op": "move", "key": "m", "id": a["id"], "name": "b.txt"})[0]
    assert not res["ok"] and "Ya existe" in res["error"]
    assert client.get(f"/api/files/{a['id']}").json()["name"] == "a.txt"


def test_trash_es_idempotente_y_un_fallo_no_corta_el_lote(client):
    register(client, "ana")
    login(client, "ana")
    a = upload(client, "a.txt", b"1")
    res = ops(
        client,
        {"op": "trash", "key": "1", "id": a["id"]},
        {"op": "trash", "key": "2", "id": a["id"]},
        {"op": "trash", "key": "3", "id": "no-existe"},
        {"op": "raro", "key": "4"},
        {"op": "mkdir", "key": "5", "parent": client.post("/api/folders", data={"name": "r"}).json()["id"], "name": "ok"},
    )
    assert [r["ok"] for r in res] == [True, True, False, False, True]
    assert res[1]["unchanged"] is True
    assert client.get(f"/api/files/{a['id']}").json()["trashed"] is True


def test_no_se_puede_tocar_lo_de_otro_usuario(make_app):
    with make_app() as client:
        register(client, "ana")
        login(client, "ana")
        ajeno = upload(client, "privado.txt", b"x")
        logout(client)
        register(client, "bea")
        login(client, "bea")
        res = ops(client, {"op": "trash", "key": "t", "id": ajeno["id"]})[0]
        assert not res["ok"]
        res = ops(client, {"op": "move", "key": "m", "id": ajeno["id"], "name": "robado"})[0]
        assert not res["ok"]
        logout(client)
        login(client, "ana")
        assert client.get(f"/api/files/{ajeno['id']}").json()["trashed"] is False


def test_lote_invalido_se_rechaza(client):
    register(client, "ana")
    login(client, "ana")
    assert client.post("/api/sync/ops", json={"ops": "nope"}).status_code == 400
    assert client.post("/api/sync/ops", json={"ops": [{"op": "trash"}] * 1001}).status_code == 400


def test_las_operaciones_dejan_rastro_en_la_auditoria(client):
    from sqlalchemy import select

    from app.database import session_scope
    from app.models import AuditLog

    register(client, "ana")
    login(client, "ana")
    raiz = client.post("/api/folders", data={"name": "raiz"}).json()["id"]
    nodo = upload(client, "a.txt", b"1", parent=raiz)
    ops(
        client,
        {"op": "mkdir", "key": "k1", "parent": raiz, "name": "sub"},
        {"op": "move", "key": "k2", "id": nodo["id"], "name": "b.txt"},
        {"op": "trash", "key": "k3", "id": nodo["id"]},
        run="pasada-42",
    )
    with session_scope() as db:
        filas = db.scalars(select(AuditLog).where(AuditLog.event.in_(["mkdir", "move", "delete"]))).all()
        vistos = {(f.event, f.detail.get("key"), f.detail.get("run")) for f in filas if f.detail.get("via") == "sync"}
    assert vistos == {("mkdir", "k1", "pasada-42"), ("move", "k2", "pasada-42"), ("delete", "k3", "pasada-42")}
