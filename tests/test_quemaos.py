"""La sonda de estado para QuemaOS: contrato, errores y que no toque la BD al responder."""

from __future__ import annotations

import json
import time
import urllib.request

import pytest
from pydantic import ValidationError

from app import quemaos
from app.config import get_config

from conftest import login, register, upload


def fetch(probe, path="/quemaos/status"):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{probe.port}{path}", timeout=3) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def probe(client):
    """Una sonda en un puerto libre (el 0 sólo vale en pruebas), sobre la instancia de prueba."""
    p = quemaos.Probe(get_config(), host="127.0.0.1", port=0)
    assert p.start()
    yield p
    p.stop()


def test_contrato(probe):
    status, body = fetch(probe)
    assert status == 200
    assert set(body) == {"quemaos", "id", "name", "status", "message", "metrics", "extra"}
    assert body["quemaos"] == 1 and body["id"] == "drive" and body["name"] == "Drive"
    assert body["status"] == "ok"
    assert fetch(probe, "/quemaos/status/")[0] == 200
    assert fetch(probe, "/otra-cosa")[0] == 404


def test_metricas_reflejan_los_datos(client, probe):
    register(client, "ana")
    login(client, "ana")
    upload(client, "a.txt", b"hola mundo")
    probe.refresh()
    m = fetch(probe)[1]["metrics"]
    assert m["usuarios"] == 1 and m["archivos"] == 1 and m["bytes_guardados"] == 10
    assert "1 usuarios · 1 archivos" in fetch(probe)[1]["message"]


def test_responder_no_toca_la_base_de_datos(probe, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("la petición no debe abrir sesión de BD")

    monkeypatch.setattr(quemaos, "session_scope", boom)
    assert fetch(probe)[0] == 200


def test_instalacion_nueva_sin_directorio_de_ficheros_esta_bien(probe):
    # Todavía no se ha subido nada: el directorio aún no existe y no es un error.
    assert fetch(probe)[1]["status"] == "ok"


def test_directorio_de_ficheros_desaparecido_es_error(client, probe):
    import shutil

    register(client, "bea")
    login(client, "bea")
    upload(client, "a.txt", b"datos")
    shutil.rmtree(get_config().storage.root)
    probe.refresh()
    body = fetch(probe)[1]
    assert body["status"] == "error" and "directorio" in body["message"]


def test_medidas_viejas_avisan_y_luego_dan_error(probe):
    probe._data["at"] = time.time() - (quemaos.STALE_WARN + 5)
    assert fetch(probe)[1]["status"] == "warn"
    probe._data["at"] = time.time() - (quemaos.STALE_ERROR + 5)
    body = fetch(probe)[1]
    assert body["status"] == "error" and "hilo de medida" in body["message"]


def test_el_puerto_ocupado_no_rompe_el_arranque(probe):
    second = quemaos.Probe(get_config(), host="127.0.0.1", port=probe.port)
    assert second.start() is False


def test_el_puerto_debe_estar_en_el_rango_de_la_suite():
    from app.config import QuemaosConfig

    assert QuemaosConfig().port == 9701
    for bad in (80, 8000, 9699, 9800):
        with pytest.raises(ValidationError):
            QuemaosConfig(port=bad)
    assert QuemaosConfig(port=9799).port == 9799
