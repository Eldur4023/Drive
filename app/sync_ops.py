"""Operaciones de sincronización por lotes: mkdir, move y trash.

El cliente de escritorio no sube nada que no sea contenido nuevo o cambiado; todo lo
demás (crear carpetas, mover, renombrar, borrar) lo manda aquí como una lista de
operaciones que el servidor reproduce de una vez, sin tráfico de ficheros.

* Cada operación es atómica y se valida entera antes de tocar nada, así que un fallo
  no deja estados a medias y el resto del lote sigue.
* Son idempotentes: repetir un lote (tras un corte) no duplica ni estropea nada.
* Dejan rastro en la auditoría con el ``run`` y la ``key`` que puso el cliente.
* Todo el lote se confirma en una sola transacción (lo hace la ruta).
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from . import audit, files_service, storage
from .auth import require_action
from .config import Config
from .models import Node, Permission, User
from .permissions import Principal
from .routers.files_routes import authorize
from .storage import StorageError

MAX_OPS = 1000


def _node(db: DbSession, principal: Principal, node_id: Any, needed: Permission) -> Node:
    node = db.get(Node, str(node_id)) if node_id else None
    if node is None:
        raise HTTPException(404, "No existe ese elemento.")
    authorize(db, principal, node, needed)
    return node


def _resolve(refs: dict[str, str], value: Any) -> Any:
    """``$ruta`` apunta a la carpeta que creó antes, en este mismo lote, el mkdir con esa ``ref``."""
    if isinstance(value, str) and value.startswith("$"):
        if value[1:] not in refs:
            raise StorageError(f"La referencia {value} no existe en este lote.")
        return refs[value[1:]]
    return value


def _sibling(
    db: DbSession, owner_id: str, parent_id: str | None, name: str, exclude: str | None = None
) -> Node | None:
    query = select(Node).where(
        Node.owner_id == owner_id,
        Node.parent_id.is_(None) if parent_id is None else Node.parent_id == parent_id,
        Node.name == name,
        Node.deleted_at.is_(None),
    )
    if exclude:
        query = query.where(Node.id != exclude)
    return db.scalars(query).first()


def _mkdir(db, principal, op, refs, config, trace):
    require_action(principal, "upload", config)
    parent = _node(db, principal, _resolve(refs, op.get("parent")), Permission.write)
    name = storage.sanitize_name(str(op.get("name", "")))
    existing = _sibling(db, parent.owner_id, parent.id, name)
    if existing is not None and not existing.is_dir:
        raise StorageError(f"Ya existe un fichero llamado «{name}».")
    created = existing is None
    if existing is None:
        owner = db.get(User, parent.owner_id)
        existing = files_service.create_folder(db, owner, parent, name)
        audit.record(db, "mkdir", principal, target_type="node", target_id=existing.id,
                     config=config, parent=parent.id, name=name, **trace)
    if op.get("ref"):
        refs[str(op["ref"])] = existing.id
    return {"id": existing.id, "unchanged": not created}


def _move(db, principal, op, refs, config, trace):
    node = _node(db, principal, op.get("id"), Permission.write)
    if node.deleted_at is not None:
        raise StorageError("Está en la papelera.")
    parent_value = _resolve(refs, op.get("parent"))
    if parent_value:
        parent = _node(db, principal, parent_value, Permission.write)
        if not parent.is_dir:
            raise StorageError("El destino no es una carpeta.")
        if parent.owner_id != node.owner_id:
            raise StorageError("Mover entre cuentas no está soportado.")
    else:  # sin «parent»: se queda donde está (puede ser la raíz del usuario)
        parent = db.get(Node, node.parent_id) if node.parent_id else None
    parent_id = parent.id if parent else None
    name = storage.sanitize_name(str(op["name"])) if op.get("name") else node.name

    if parent_id == node.parent_id and name == node.name:
        return {"id": node.id, "unchanged": True}
    if _sibling(db, node.owner_id, parent_id, name, exclude=node.id) is not None:
        raise StorageError(f"Ya existe «{name}» en el destino.")

    before = {"from_parent": node.parent_id, "from_name": node.name}
    if parent_id != node.parent_id:
        files_service.move(db, node, parent)
    if node.name != name:
        files_service.rename(db, node, name)
    audit.record(db, "move", principal, target_type="node", target_id=node.id, config=config,
                 to_parent=parent_id, to_name=node.name, **before, **trace)
    return {"id": node.id, "unchanged": False}


def _trash(db, principal, op, refs, config, trace):
    require_action(principal, "delete", config)
    node = _node(db, principal, op.get("id"), Permission.write)
    if node.deleted_at is not None:
        return {"id": node.id, "unchanged": True}
    audit.record(db, "delete", principal, target_type="node", target_id=node.id,
                 config=config, permanent=False, **trace)
    files_service.trash(db, node, config)
    return {"id": node.id, "unchanged": False}


_OPS = {"mkdir": _mkdir, "move": _move, "trash": _trash}


def apply_ops(
    db: DbSession, principal: Principal, ops: list[dict[str, Any]], run: str, config: Config
) -> list[dict[str, Any]]:
    """Aplica el lote en orden. Devuelve un resultado por operación, en el mismo orden."""
    refs: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    for index, op in enumerate(ops):
        key = str(op.get("key", index))
        handler = _OPS.get(str(op.get("op")))
        try:
            if handler is None:
                raise StorageError(f"Operación desconocida: {op.get('op')!r}.")
            out = handler(db, principal, op, refs, config, {"run": run, "key": key, "via": "sync"})
            results.append({"key": key, "ok": True, **out})
        except HTTPException as exc:
            results.append({"key": key, "ok": False, "error": str(exc.detail)})
        except StorageError as exc:
            results.append({"key": key, "ok": False, "error": str(exc)})
    return results
