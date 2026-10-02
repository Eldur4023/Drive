"""Panel de administración, perfiles por rol y ajustes en caliente."""

from __future__ import annotations

from conftest import login, logout, register, upload


def test_solo_admin_entra_al_panel(client):
    register(client, "jefa")
    register(client, "normal")
    login(client, "normal")
    assert client.get("/admin", follow_redirects=False).status_code == 403

    logout(client)
    login(client, "jefa")
    assert client.get("/admin").status_code == 200


def test_admin_crea_usuarios(client):
    register(client, "jefa")
    login(client, "jefa")

    response = client.post(
        "/admin/users",
        data={
            "username": "nuevo",
            "email": "nuevo@example.com",
            "password": "Contrasena1",
            "role": "user",
            "quota": "5GB",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    logout(client)

    login(client, "nuevo")
    assert client.get("/api/me").json()["quota"] == 5 * 1024**3


def test_perfil_guest_no_puede_subir(client):
    register(client, "jefa")
    login(client, "jefa")
    client.post(
        "/admin/users",
        data={"username": "invitado", "password": "Contrasena1", "role": "guest", "email": ""},
        follow_redirects=False,
    )
    logout(client)

    login(client, "invitado")
    response = client.post("/api/files", files={"file": ("x.txt", b"x", "text/plain")})
    assert response.status_code == 403
    assert "perfil" in response.json()["detail"]


def test_override_individual_gana_al_rol(client):
    register(client, "jefa")
    login(client, "jefa")
    client.post(
        "/admin/users",
        data={"username": "invitado", "password": "Contrasena1", "role": "guest", "email": ""},
        follow_redirects=False,
    )

    users = client.get("/admin/users").text
    assert "invitado" in users

    # Se le concede subir aunque su rol no lo permita.
    import re

    ids = re.findall(r'action="/admin/users/([0-9a-f]{32})"', users)
    assert ids
    for user_id in ids:
        client.post(
            f"/admin/users/{user_id}",
            data={
                "role": "guest",
                "is_active": "true",
                "is_approved": "true",
                "can_upload": "yes",
                "can_delete": "inherit",
                "can_share_public": "inherit",
                "can_share_internal": "inherit",
            },
            follow_redirects=False,
        )
    logout(client)

    login(client, "invitado")
    response = client.post("/api/files", files={"file": ("x.txt", b"x", "text/plain")})
    assert response.status_code == 201


def test_ajuste_en_caliente_desde_el_panel(client):
    register(client, "jefa")
    login(client, "jefa")

    # Desactivar los enlaces públicos debe surtir efecto sin reiniciar.
    node = upload(client, "algo.txt", b"x")
    assert client.post(f"/api/files/{node['id']}/links", data={}).status_code == 201

    response = client.post(
        "/admin/settings",
        data={"sharing__public_links_enabled": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.post(f"/api/files/{node['id']}/links", data={}).status_code == 403


def test_ajuste_invalido_se_rechaza(client):
    register(client, "jefa")
    login(client, "jefa")
    response = client.post(
        "/admin/settings",
        data={"network__trusted_mode": "modo-inventado"},
        follow_redirects=True,
    )
    # El valor no se aplica y la instancia sigue funcionando.
    assert client.get("/api/me").json()["authenticated"] is True
    assert "error" in response.url.query.decode() or response.status_code == 200


def test_no_se_puede_quedar_sin_administradores(client):
    register(client, "jefa")
    login(client, "jefa")
    import re

    ids = re.findall(r'action="/admin/users/([0-9a-f]{32})"', client.get("/admin/users").text)
    response = client.post(
        f"/admin/users/{ids[0]}",
        data={"role": "user", "is_active": "true", "is_approved": "true"},
        follow_redirects=True,
    )
    assert "al menos un administrador" in response.text
    assert client.get("/api/me").json()["role"] == "admin"


def test_auditoria_registra_los_accesos(client):
    register(client, "jefa")
    login(client, "jefa")
    node = upload(client, "auditado.txt", b"x")
    client.get(f"/files/{node['id']}/download")

    registro = client.get("/admin/audit").text
    assert "upload" in registro
    assert "download" in registro


def test_token_de_api_respeta_sus_ambitos(client):
    register(client, "ana")
    login(client, "ana")

    response = client.post(
        "/profile/tokens",
        data={"name": "solo-lectura", "scopes": ["read"], "expires_days": 0},
        follow_redirects=False,
    )
    token = response.headers["location"].split("new_token=")[-1]
    client.post("/logout", follow_redirects=False)

    cabecera = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/me", headers=cabecera).json()["username"] == "ana"
    assert client.get("/api/files", headers=cabecera).status_code == 200

    escritura = client.post(
        "/api/files", headers=cabecera, files={"file": ("x.txt", b"x", "text/plain")}
    )
    assert escritura.status_code == 403
