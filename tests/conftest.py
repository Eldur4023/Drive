"""Fixtures comunes: una instancia limpia por test, en un directorio temporal."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient


BASE_CONFIG = {
    "app": {"base_url": "http://testserver"},
    "security": {
        "secret_key": "clave-de-pruebas-suficientemente-larga-0123456789",
        "password_policy": {"min_length": 8, "require_symbol": False},
        "totp": {"enabled": True, "required_for_roles": []},
    },
    "auth": {"registration_enabled": True, "require_admin_approval": False},
    "rate_limit": {"enabled": False},
    "maintenance": {"enabled": False},
    "network": {"trusted_networks": [], "trusted_mode": "none"},
    "server": {"behind_proxy": False},
    "audit": {"enabled": True},
}


def _write_config(tmp_path: Path, overrides: dict | None = None) -> Path:
    from app.config import deep_merge

    data = deep_merge(BASE_CONFIG, overrides or {})
    data.setdefault("database", {})["url"] = f"sqlite:///{tmp_path / 'test.db'}".replace("\\", "/")
    data.setdefault("storage", {})["root"] = str(tmp_path / "blobs")

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _with_client_ip(asgi_app, ip: str):
    """Envuelve la aplicación para fijar la IP de origen de cada petición."""

    async def wrapper(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (ip, 45000)
        await asgi_app(scope, receive, send)

    return wrapper


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """Construye una aplicación aislada, opcionalmente con otra configuración."""

    def factory(
        overrides: dict | None = None, client_ip: str = "203.0.113.9"
    ) -> TestClient:
        path = _write_config(tmp_path, overrides)
        monkeypatch.setenv("DRIVE_CONFIG", str(path))
        # Las variables DRIVE_* de la máquina anfitriona no deben filtrarse.
        for key in list(os.environ):
            if key.startswith("DRIVE_") and key != "DRIVE_CONFIG":
                monkeypatch.delenv(key, raising=False)

        # Los módulos guardan estado global (motor, configuración activa), así
        # que se recargan para que cada test parta de cero.
        import app.config
        import app.database

        importlib.reload(app.config)
        importlib.reload(app.database)
        import app.main

        importlib.reload(app.main)
        # La IP de origen decide el acceso sin login, así que hay que poder
        # simularla.
        return TestClient(_with_client_ip(app.main.create_app(), client_ip))

    return factory


@pytest.fixture
def client(make_app):
    with make_app() as test_client:
        yield test_client


def register(client: TestClient, username: str, password: str = "Contrasena1") -> None:
    response = client.post(
        "/register",
        data={
            "username": username,
            "email": f"{username}@example.com",
            "password": password,
            "password2": password,
        },
        follow_redirects=False,
    )
    assert response.status_code in (200, 303), response.text


def login(client: TestClient, username: str, password: str = "Contrasena1") -> None:
    response = client.post(
        "/login",
        data={"username": username, "password": password, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def logout(client: TestClient) -> None:
    client.post("/logout", follow_redirects=False)


def upload(client: TestClient, name: str, content: bytes, parent: str = "") -> dict:
    response = client.post(
        "/api/files",
        data={"parent_id": parent},
        files={"file": (name, content, "text/plain")},
    )
    assert response.status_code == 201, response.text
    return response.json()
