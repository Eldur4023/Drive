"""Acceso sin login desde redes de confianza, y suplantación de cabeceras."""

from __future__ import annotations

from conftest import login, logout, register, upload

LAN = {"network": {"trusted_networks": ["192.168.1.0/24"]}}


def _publicar_en_lan(client, nombre: str = "compartido-lan.txt", contenido: bytes = b"lan"):
    """Sube algo y lo marca visible para la red local."""
    register(client, "ana")
    login(client, "ana")
    node = upload(client, nombre, contenido)
    client.patch(f"/api/files/{node['id']}", data={"lan_visible": "true"})
    logout(client)
    return node


def test_modo_none_pide_login_siempre(make_app):
    config = {"network": {**LAN["network"], "trusted_mode": "none"}}
    with make_app(config, client_ip="192.168.1.50") as client:
        node = _publicar_en_lan(client)
        assert client.get(f"/p/{node['id']}").status_code == 404


def test_modo_readonly_permite_leer_sin_cuenta(make_app):
    config = {
        "network": {**LAN["network"], "trusted_mode": "readonly", "trusted_scope": "shared"}
    }
    with make_app(config, client_ip="192.168.1.50") as client:
        node = _publicar_en_lan(client)

        assert client.get(f"/p/{node['id']}").status_code == 200
        assert client.get(f"/p/{node['id']}/download").content == b"lan"

        estado = client.get("/api/me").json()
        assert estado["authenticated"] is False
        assert estado["trusted_network"] is True
        assert estado["readonly"] is True


def test_desde_fuera_de_la_lan_no_se_ve(make_app):
    config = {
        "network": {**LAN["network"], "trusted_mode": "readonly", "trusted_scope": "shared"}
    }
    with make_app(config, client_ip="203.0.113.7") as client:
        node = _publicar_en_lan(client)
        assert client.get(f"/p/{node['id']}").status_code == 404
        assert client.get("/api/me").json()["trusted_network"] is False


def test_modo_full_abre_sesion_automatica(make_app):
    config = {
        "network": {
            **LAN["network"],
            "trusted_mode": "full",
            "trusted_user": "ana",
            "trusted_scope": "trusted_user",
            "require_login_for": ["admin", "settings"],
        }
    }
    with make_app(config, client_ip="192.168.1.50") as client:
        register(client, "ana")
        login(client, "ana")
        node = upload(client, "mio.txt", b"contenido de ana")
        logout(client)

        estado = client.get("/api/me").json()
        assert estado["authenticated"] is True
        assert estado["username"] == "ana"
        assert estado["kind"] == "network"

        assert client.get(f"/files/{node['id']}/download").content == b"contenido de ana"


def test_modo_full_respeta_require_login_for(make_app):
    config = {
        "network": {
            **LAN["network"],
            "trusted_mode": "full",
            "trusted_user": "ana",
            "trusted_scope": "trusted_user",
            "require_login_for": ["upload", "delete", "share", "admin", "settings"],
        }
    }
    with make_app(config, client_ip="192.168.1.50") as client:
        register(client, "ana")
        logout(client)

        # Puede leer, pero subir exige identificarse de verdad.
        assert client.get("/api/me").json()["authenticated"] is True
        response = client.post("/api/files", files={"file": ("x.txt", b"x", "text/plain")})
        assert response.status_code == 403
        assert "iniciar sesión" in response.json()["detail"]


def test_no_se_confia_en_x_forwarded_for_ajeno(make_app):
    """Sin proxy declarado, la cabecera no debe poder falsear la red."""
    config = {
        "network": {**LAN["network"], "trusted_mode": "readonly", "trusted_scope": "shared"},
        "server": {"behind_proxy": False},
    }
    with make_app(config, client_ip="203.0.113.7") as client:
        node = _publicar_en_lan(client)
        response = client.get(
            f"/p/{node['id']}", headers={"X-Forwarded-For": "192.168.1.50"}
        )
        assert response.status_code == 404


def test_x_forwarded_for_se_acepta_desde_un_proxy_declarado(make_app):
    config = {
        "network": {**LAN["network"], "trusted_mode": "readonly", "trusted_scope": "shared"},
        "server": {"behind_proxy": True, "trusted_proxies": ["203.0.113.7/32"]},
    }
    with make_app(config, client_ip="203.0.113.7") as client:
        node = _publicar_en_lan(client)
        response = client.get(
            f"/p/{node['id']}", headers={"X-Forwarded-For": "192.168.1.50"}
        )
        assert response.status_code == 200


def test_redes_denegadas(make_app):
    config = {"network": {"denied_networks": ["192.168.1.0/24"]}}
    with make_app(config, client_ip="192.168.1.50") as client:
        assert client.get("/browse").status_code == 403


def test_lo_privado_no_se_expone_a_la_lan(make_app):
    """Estar en la red de confianza no da acceso a lo que nadie ha abierto."""
    config = {
        "network": {**LAN["network"], "trusted_mode": "readonly", "trusted_scope": "shared"}
    }
    with make_app(config, client_ip="192.168.1.50") as client:
        register(client, "ana")
        login(client, "ana")
        node = upload(client, "privado.txt", b"no compartido")
        logout(client)

        assert client.get(f"/p/{node['id']}").status_code == 404
