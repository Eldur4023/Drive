"""API JSON para clientes no interactivos.

Se autentica con ``Authorization: Bearer <token>`` (ver el perfil de usuario)
o con la cookie de sesión, de modo que la misma API sirve para scripts y para
el propio navegador.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import audit, files_service, shares_service, storage
from ..auth import get_principal, require_action, require_user
from ..config import get_config
from ..database import get_db
from ..models import LinkMode, Node, Permission, User
from ..permissions import Principal, path_of
from ..storage import StorageError
from .files_routes import authorize, get_node, serve_blob

router = APIRouter(prefix="/api", tags=["api"])


def _needs_scope(principal: Principal, scope: str) -> None:
    """Los tokens de API sólo pueden lo que declaran sus ámbitos."""
    if principal.kind == "token" and scope not in principal.scopes:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"El token no tiene el ámbito '{scope}'."
        )


def node_json(node: Node, access: Any = None) -> dict[str, Any]:
    data = {
        "id": node.id,
        "name": node.name,
        "is_dir": node.is_dir,
        "size": node.size,
        "mime_type": node.mime_type,
        "created_at": node.created_at.isoformat(),
        "updated_at": node.updated_at.isoformat(),
        "is_public": node.is_public,
        "lan_visible": node.lan_visible,
        "starred": node.starred,
        "trashed": node.is_trashed,
        "parent_id": node.parent_id,
        "owner_id": node.owner_id,
    }
    if access is not None:
        data["permission"] = access.permission.value if access.permission else None
        data["access_via"] = access.source
    return data


# --------------------------------------------------------------------------- #
# Sesión
# --------------------------------------------------------------------------- #


@router.get("/me")
def me(principal: Principal = Depends(get_principal)):
    config = get_config()
    if principal.user is None:
        return {
            "authenticated": False,
            "kind": principal.kind,
            "trusted_network": principal.trusted_network,
            "readonly": principal.readonly,
        }
    usage = files_service.usage_of(principal.user, config)
    return {
        "authenticated": True,
        "kind": principal.kind,
        "id": principal.user.id,
        "username": principal.user.username,
        "display_name": principal.user.label,
        "email": principal.user.email,
        "role": principal.user.role.value,
        "trusted_network": principal.trusted_network,
        "readonly": principal.readonly,
        "scopes": principal.scopes,
        "quota": usage.quota,
        "used": usage.used,
        "max_upload_size": files_service.max_upload_of(principal.user, config),
    }


# --------------------------------------------------------------------------- #
# Navegación
# --------------------------------------------------------------------------- #


@router.get("/files")
def list_root(
    parent_id: str | None = None,
    order: str = "name",
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    _needs_scope(principal, "read")
    user = principal.user
    assert user is not None

    owner_id = user.id
    if parent_id:
        parent = get_node(db, parent_id)
        authorize(db, principal, parent, Permission.read)
        owner_id = parent.owner_id

    items = files_service.children_of(db, owner_id, parent_id, order=order)
    sizes = files_service.folder_sizes(db, owner_id)
    data = [node_json(n) for n in items]
    for d in data:
        if d["is_dir"]:
            d["size"] = sizes.get(d["id"], 0)
    return {"parent_id": parent_id, "items": data}


@router.get("/files/{node_id}")
def get_file(
    node_id: str,
    principal: Principal = Depends(get_principal),
    db: DbSession = Depends(get_db),
):
    _needs_scope(principal, "read")
    node = get_node(db, node_id)
    access = authorize(db, principal, node, Permission.read)
    data = node_json(node, access)
    data["path"] = [{"id": n.id, "name": n.name} for n in path_of(db, node)]
    if node.is_dir:
        data["children"] = [
            node_json(child) for child in files_service.children_of(db, node.owner_id, node.id)
        ]
    return data


@router.get("/files/{node_id}/content")
def get_content(
    request: Request,
    node_id: str,
    principal: Principal = Depends(get_principal),
    db: DbSession = Depends(get_db),
):
    _needs_scope(principal, "read")
    config = get_config()
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.read)
    require_action(principal, "download", config)
    audit.record(db, "download", principal, target_type="node", target_id=node.id,
                 via="api", config=config)
    db.commit()
    return serve_blob(request, node, inline=False, config=config)


@router.get("/search")
def api_search(
    q: str,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    _needs_scope(principal, "read")
    results = files_service.search(db, principal.user.id, q)  # type: ignore[union-attr]
    return {"query": q, "items": [node_json(n) for n in results]}


@router.get("/shared")
def api_shared(
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    _needs_scope(principal, "read")
    nodes = files_service.shared_with(db, principal.user.id)  # type: ignore[union-attr]
    return {"items": [node_json(n) for n in nodes]}


@router.get("/trash")
def api_trash(
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    _needs_scope(principal, "read")
    nodes = files_service.trashed_of(db, principal.user.id)  # type: ignore[union-attr]
    return {"items": [node_json(n) for n in nodes]}


# --------------------------------------------------------------------------- #
# Mutaciones
# --------------------------------------------------------------------------- #


@router.post("/files", status_code=status.HTTP_201_CREATED)
def api_upload(
    parent_id: str = Form(""),
    overwrite: bool = Form(False),
    relpath: str = Form(""),  # ruta relativa si se sube desde una carpeta
    file: UploadFile = File(...),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    _needs_scope(principal, "write")
    config = get_config()
    require_action(principal, "upload", config)
    user = principal.user
    assert user is not None

    parent = None
    owner = user
    if parent_id:
        parent = get_node(db, parent_id)
        authorize(db, principal, parent, Permission.write)
        owner = db.get(User, parent.owner_id) or user

    try:
        node = files_service.save_upload(
            db, owner, files_service.ensure_folders(db, owner, parent, relpath),
            file.filename or "sin-nombre", file.file,
            overwrite=overwrite, config=config,
        )
    except StorageError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    finally:
        file.file.close()

    audit.record(db, "upload", principal, target_type="node", target_id=node.id,
                 via="api", name=node.name, size=node.size, config=config)
    db.commit()
    return node_json(node)


@router.post("/folders", status_code=status.HTTP_201_CREATED)
def api_mkdir(
    name: str = Form(...),
    parent_id: str = Form(""),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    _needs_scope(principal, "write")
    config = get_config()
    require_action(principal, "upload", config)
    user = principal.user
    assert user is not None

    parent = None
    owner = user
    if parent_id:
        parent = get_node(db, parent_id)
        authorize(db, principal, parent, Permission.write)
        owner = db.get(User, parent.owner_id) or user

    node = files_service.create_folder(db, owner, parent, name)
    db.commit()
    return node_json(node)


@router.patch("/files/{node_id}")
def api_update(
    node_id: str,
    name: str | None = Form(None),
    parent_id: str | None = Form(None),
    is_public: bool | None = Form(None),
    lan_visible: bool | None = Form(None),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    _needs_scope(principal, "write")
    config = get_config()
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.write)

    try:
        if name:
            files_service.rename(db, node, name)
        if parent_id is not None:
            target = get_node(db, parent_id) if parent_id else None
            if target is not None:
                authorize(db, principal, target, Permission.write)
            files_service.move(db, node, target)
    except StorageError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if is_public is not None or lan_visible is not None:
        authorize(db, principal, node, Permission.owner)
        if is_public:
            require_action(principal, "share_public", config)
        if is_public is not None:
            node.is_public = is_public
        if lan_visible is not None:
            node.lan_visible = lan_visible

    db.commit()
    return node_json(node)


@router.delete("/files/{node_id}")
def api_delete(
    node_id: str,
    permanent: bool = False,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    _needs_scope(principal, "write")
    config = get_config()
    require_action(principal, "delete", config)
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.owner if permanent else Permission.write)

    audit.record(db, "delete", principal, target_type="node", target_id=node.id,
                 via="api", permanent=permanent, config=config)
    if permanent:
        files_service.purge(db, node, config)
    else:
        files_service.trash(db, node, config)
    db.commit()
    return {"deleted": node_id, "permanent": permanent}


@router.post("/files/{node_id}/restore")
def api_restore(
    node_id: str,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    _needs_scope(principal, "write")
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.write)
    try:
        files_service.restore(db, node, get_config())
    except StorageError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    return node_json(node)


# --------------------------------------------------------------------------- #
# Compartición
# --------------------------------------------------------------------------- #


@router.post("/files/{node_id}/shares", status_code=status.HTTP_201_CREATED)
def api_share(
    node_id: str,
    username: str = Form(...),
    permission: str = Form("read"),
    expires_days: int = Form(0),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    _needs_scope(principal, "share")
    config = get_config()
    require_action(principal, "share", config)
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.owner)

    target = db.scalar(select(User).where(User.username == username.strip()))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese usuario.")

    try:
        share = shares_service.share_with_user(
            db, node, target, Permission(permission), principal.user,  # type: ignore[arg-type]
            expires_in=expires_days * 86400 if expires_days else None, config=config,
        )
    except (StorageError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    audit.record(db, "share_create", principal, target_type="node", target_id=node.id,
                 kind="user", to=target.username, via="api", config=config)
    db.commit()
    return {"id": share.id, "user": target.username, "permission": share.permission.value}


@router.post("/files/{node_id}/links", status_code=status.HTTP_201_CREATED)
def api_link(
    node_id: str,
    mode: str = Form("download"),
    password: str = Form(""),
    expires_days: int = Form(0),
    download_limit: int = Form(0),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    _needs_scope(principal, "share")
    config = get_config()
    require_action(principal, "share_public", config)
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.owner)

    try:
        link = shares_service.create_link(
            db, node, principal.user,  # type: ignore[arg-type]
            mode=LinkMode(mode), password=password or None,
            expires_in=expires_days * 86400 if expires_days else None,
            download_limit=download_limit or None, config=config,
        )
    except (StorageError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    audit.record(db, "share_create", principal, target_type="node", target_id=node.id,
                 kind="link", via="api", config=config)
    db.commit()
    return {
        "id": link.id,
        "url": shares_service.link_url(link, config),
        "expires_at": link.expires_at.isoformat() if link.expires_at else None,
        "mode": link.mode.value,
    }


@router.get("/sync/tree")
def sync_tree(
    root_id: str | None = None,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    """Todo lo vivo bajo una carpeta (o la raíz del usuario), plano y con rutas.

    Es lo que necesita un cliente de sincronización para comparar de una vez, en
    lugar de recorrer carpeta a carpeta. ``hash`` es el del contenido (nulo en
    carpetas); lo que está en la papelera no aparece, de modo que para el cliente
    «ausente» significa «borrado».
    """
    _needs_scope(principal, "read")
    user = principal.user
    assert user is not None

    owner_id = user.id
    if root_id:
        root = get_node(db, root_id)
        authorize(db, principal, root, Permission.read)
        if not root.is_dir:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "La raíz no es una carpeta.")
        owner_id = root.owner_id

    nodes = db.scalars(
        select(Node).where(Node.owner_id == owner_id, Node.deleted_at.is_(None))
    ).all()
    children: dict[str | None, list[Node]] = {}
    for node in nodes:
        children.setdefault(node.parent_id, []).append(node)

    items: list[dict[str, Any]] = []
    stack = [(root_id or None, "")]
    while stack:
        parent_id, prefix = stack.pop()
        for node in sorted(children.get(parent_id, []), key=lambda n: n.name):
            path = prefix + node.name
            items.append({
                "id": node.id,
                "path": path,
                "is_dir": node.is_dir,
                "size": node.size,
                "hash": node.blob_hash,
                "updated_at": node.updated_at.replace(tzinfo=timezone.utc).timestamp(),
            })
            if node.is_dir:
                stack.append((node.id, path + "/"))
    return {
        "root_id": root_id,
        "hash_algorithm": get_config().storage.hash_algorithm,
        "items": items,
    }


@router.get("/usage")
def api_usage(
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    _needs_scope(principal, "read")
    config = get_config()
    usage = files_service.usage_of(principal.user, config)  # type: ignore[arg-type]
    return {
        "used": usage.used,
        "used_human": storage.format_size(usage.used),
        "quota": usage.quota,
        "quota_human": storage.format_size(usage.quota),
        "percent": usage.percent,
    }
