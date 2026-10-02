"""Ajustes editables en caliente desde el panel de administración.

Cada override se guarda como una fila ``clave -> valor`` donde la clave es la
ruta con puntos dentro del YAML (``network.trusted_mode``). Al aplicarlos se
reconstruye el objeto :class:`~app.config.Config` completo, de modo que un
valor inválido falla al validar y se rechaza antes de quedar activo.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Config, load_config, set_active_config
from .models import SettingOverride

log = logging.getLogger(__name__)

#: Claves que sólo pueden tocarse en el YAML o por entorno. Cambiarlas en
#: caliente rompería el proceso (rutas, motor de BD) o permitiría a un admin
#: comprometido reescribir el secreto de firma.
LOCKED_KEYS = {
    "database.url",
    "database.pool_size",
    "database.max_overflow",
    "storage.root",
    "storage.hash_algorithm",
    "security.secret_key",
    "security.password_hash",
    "server.host",
    "server.port",
    "server.workers",
}


def _explode(key: str, value: Any) -> dict[str, Any]:
    """``"a.b.c", 1`` -> ``{"a": {"b": {"c": 1}}}``."""
    parts = key.split(".")
    result: dict[str, Any] = {}
    cursor = result
    for part in parts[:-1]:
        cursor[part] = {}
        cursor = cursor[part]
    cursor[parts[-1]] = value
    return result


def load_overrides(db: Session) -> dict[str, Any]:
    from .config import deep_merge

    merged: dict[str, Any] = {}
    for row in db.scalars(select(SettingOverride)):
        if row.key in LOCKED_KEYS:
            continue
        merged = deep_merge(merged, _explode(row.key, row.value.get("v")))
    return merged


def apply_overrides(db: Session) -> Config:
    """Recarga la configuración activa aplicando lo guardado en la BD."""
    config = load_config(overrides=load_overrides(db))
    set_active_config(config)
    return config


def set_override(db: Session, key: str, value: Any, user_id: str | None = None) -> Config:
    """Guarda un ajuste y lo activa. Lanza ``ValueError`` si no es válido."""
    if key in LOCKED_KEYS:
        raise ValueError(
            f"'{key}' sólo puede cambiarse en config.yaml o por variable de entorno"
        )

    previous = db.get(SettingOverride, key)
    row = previous or SettingOverride(key=key)
    old_value = previous.value if previous else None
    row.value = {"v": value}
    row.updated_by = user_id
    db.add(row)
    db.flush()

    try:
        config = apply_overrides(db)
    except Exception as exc:
        # No dejamos en la BD un valor que la aplicación no puede cargar.
        db.rollback()
        if old_value is not None:
            db.merge(SettingOverride(key=key, value=old_value, updated_by=user_id))
            db.commit()
        apply_overrides(db)
        raise ValueError(f"valor inválido para '{key}': {exc}") from exc

    db.commit()
    return config


def clear_override(db: Session, key: str) -> Config:
    """Elimina un override y vuelve al valor del YAML."""
    row = db.get(SettingOverride, key)
    if row is not None:
        db.delete(row)
        db.flush()
    config = apply_overrides(db)
    db.commit()
    return config


def get_value(config: Config, key: str) -> Any:
    """Lee una clave con notación de puntos de la configuración activa."""
    cursor: Any = config
    for part in key.split("."):
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        else:
            cursor = getattr(cursor, part, None)
        if cursor is None:
            return None
    return cursor
