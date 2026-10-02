"""Registro, sesión, política de contraseñas y bloqueo."""

from __future__ import annotations

from conftest import login, register


def test_primera_cuenta_es_admin(client):
    register(client, "primero")
    login(client, "primero")
    assert client.get("/api/me").json()["role"] == "admin"


def test_segunda_cuenta_usa_el_rol_por_defecto(client):
    register(client, "primero")
    register(client, "segundo")
    login(client, "segundo")
    assert client.get("/api/me").json()["role"] == "user"


def test_password_debe_cumplir_la_politica(client):
    response = client.post(
        "/register",
        data={"username": "corto", "email": "", "password": "abc", "password2": "abc"},
    )
    assert response.status_code == 400
    assert "8 caracteres" in response.text


def test_credenciales_incorrectas(client):
    register(client, "alguien")
    response = client.post(
        "/login",
        data={"username": "alguien", "password": "loQueSea1", "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert "incorrectos" in response.text


def test_sin_sesion_redirige_al_login(client):
    response = client.get("/files", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_logout_invalida_la_sesion(client):
    register(client, "usuario")
    login(client, "usuario")
    assert client.get("/api/me").json()["authenticated"] is True

    client.post("/logout", follow_redirects=False)
    assert client.get("/api/me").json()["authenticated"] is False


def test_bloqueo_tras_varios_fallos(make_app):
    with make_app({"security": {"lockout": {"max_attempts": 3, "window": 900}}}) as client:
        register(client, "victima")
        for _ in range(3):
            client.post(
                "/login",
                data={"username": "victima", "password": "malaClave9", "next": "/"},
                follow_redirects=False,
            )
        # La contraseña correcta tampoco entra mientras dure el bloqueo.
        response = client.post(
            "/login",
            data={"username": "victima", "password": "Contrasena1", "next": "/"},
            follow_redirects=False,
        )
        assert response.status_code == 401
        assert "Demasiados intentos" in response.text


def test_registro_cerrado(make_app):
    with make_app({"auth": {"registration_enabled": False}}) as client:
        register(client, "elprimero")  # la primera cuenta siempre se permite
        response = client.get("/register")
        assert response.status_code == 403


def test_cuenta_pendiente_de_aprobacion(make_app):
    with make_app({"auth": {"require_admin_approval": True}}) as client:
        register(client, "jefe")
        response = client.post(
            "/register",
            data={
                "username": "pendiente",
                "email": "",
                "password": "Contrasena1",
                "password2": "Contrasena1",
            },
        )
        assert "pendiente" in response.text

        response = client.post(
            "/login",
            data={"username": "pendiente", "password": "Contrasena1", "next": "/"},
            follow_redirects=False,
        )
        assert response.status_code == 401
        assert "aprobación" in response.text


def test_cambio_de_password_cierra_las_demas_sesiones(client, make_app):
    register(client, "usuario")
    login(client, "usuario")

    response = client.post(
        "/profile/password",
        data={"current": "Contrasena1", "password": "NuevaClave9", "password2": "NuevaClave9"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # La sesión actual sobrevive al cambio.
    assert client.get("/api/me").json()["authenticated"] is True
