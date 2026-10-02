"""Navegación, subida y descarga de ficheros."""

from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import audit, files_service, ratelimit, storage, thumbnails
from ..auth import get_principal, require_action, require_user
from ..config import get_config
from ..database import get_db
from ..models import Node, NodeVersion, Permission, User
from ..permissions import Access, Principal, path_of, resolve
from ..shares_service import links_of, shares_of
from ..storage import StorageError
from ..templating import render

router = APIRouter(tags=["files"])


# --------------------------------------------------------------------------- #
# Utilidades comunes
# --------------------------------------------------------------------------- #


def get_node(db: DbSession, node_id: str) -> Node:
    node = db.get(Node, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese elemento.")
    return node


def authorize(
    db: DbSession, principal: Principal, node: Node, needed: Permission
) -> Access:
    """Comprueba el permiso sobre un nodo o corta la petición.

    A quien no tiene ningún acceso se le responde 404 en lugar de 403: así la
    existencia de un fichero ajeno no se puede confirmar sondeando ids.
    """
    access = resolve(db, principal, node)
    if not access.granted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese elemento.")
    if not access.allows(needed):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "No tienes permiso suficiente sobre este elemento."
        )
    return access


def _back(node: Node | None, message: str | None = None, error: str | None = None) -> RedirectResponse:
    target = f"/files/{node.id}" if node else "/files"
    params = {}
    if message:
        params["ok"] = message
    if error:
        params["error"] = error
    if params:
        target += "?" + urllib.parse.urlencode(params)
    return RedirectResponse(target, status.HTTP_303_SEE_OTHER)


def _content_disposition(name: str, inline: bool) -> str:
    """Cabecera con el nombre original, escapado para nombres no ASCII."""
    quoted = urllib.parse.quote(name)
    kind = "inline" if inline else "attachment"
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace('"', "'")
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


def serve_blob(
    request: Request, node: Node, *, inline: bool, config=None
) -> Response:
    """Envía el contenido de un fichero, con soporte de ``Range`` y ``ETag``."""
    config = config or get_config()
    if node.is_dir or not node.blob_hash:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Esto no es un fichero.")

    etag = f'"{node.blob_hash}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})

    headers = {
        "ETag": etag,
        "Accept-Ranges": "bytes",
        "Content-Disposition": _content_disposition(node.name, inline),
        # Los blobs son inmutables: el navegador puede cachearlos sin miedo,
        # pero sólo en su almacenamiento privado.
        "Cache-Control": "private, max-age=86400",
    }
    media_type = node.mime_type or "application/octet-stream"
    if inline and media_type in ("text/html", "image/svg+xml", "application/xhtml+xml"):
        # Evita que un HTML subido por un usuario se ejecute en el origen.
        media_type = "text/plain; charset=utf-8"

    rng = storage.parse_range(request.headers.get("range"), node.size)
    if rng is not None:
        start, end = rng
        headers["Content-Range"] = f"bytes {start}-{end}/{node.size}"
        headers["Content-Length"] = str(end - start + 1)
        return StreamingResponse(
            storage.iter_blob(node.blob_hash, start, end, config),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            media_type=media_type,
            headers=headers,
        )

    headers["Content-Length"] = str(node.size)
    return StreamingResponse(
        storage.iter_blob(node.blob_hash, config=config),
        media_type=media_type,
        headers=headers,
    )


# --------------------------------------------------------------------------- #
# Navegación
# --------------------------------------------------------------------------- #


@router.get("/")
def home(principal: Principal = Depends(get_principal)):
    if principal.is_authenticated:
        return RedirectResponse("/files", status.HTTP_303_SEE_OTHER)
    return RedirectResponse("/browse", status.HTTP_303_SEE_OTHER)


@router.get("/files")
@router.get("/files/{node_id}")
def browse(
    request: Request,
    node_id: str | None = None,
    order: str = "name",
    ok: str | None = None,
    error: str | None = None,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    user = principal.user
    assert user is not None

    current: Node | None = None
    breadcrumbs: list[Node] = []
    if node_id:
        current = get_node(db, node_id)
        authorize(db, principal, current, Permission.read)
        if not current.is_dir:
            return RedirectResponse(f"/files/{current.id}/detail", status.HTTP_303_SEE_OTHER)
        breadcrumbs = path_of(db, current)

    # Al abrir una carpeta ajena compartida se listan sus hijos; en la raíz,
    # los del propio usuario.
    owner_id = current.owner_id if current else user.id
    items = files_service.children_of(db, owner_id, current.id if current else None, order=order)

    return render(
        request,
        "files.html",
        principal,
        current=current,
        breadcrumbs=breadcrumbs,
        items=items,
        order=order,
        usage=files_service.usage_of(user, config),
        shared_count=len(files_service.shared_with(db, user.id)),
        ok=ok,
        error=error,
    )


@router.get("/shared")
def shared(
    request: Request,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    user = principal.user
    assert user is not None
    nodes = files_service.shared_with(db, user.id)
    owners = {n.owner_id: db.get(User, n.owner_id) for n in nodes}
    return render(request, "shared.html", principal, items=nodes, owners=owners)


@router.get("/trash")
def trash_view(
    request: Request,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    user = principal.user
    assert user is not None
    return render(
        request,
        "trash.html",
        principal,
        items=files_service.trashed_of(db, user.id),
        retention=get_config().trash.retention,
    )


@router.get("/search")
def search(
    request: Request,
    q: str = "",
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    user = principal.user
    assert user is not None
    results = files_service.search(db, user.id, q) if q.strip() else []
    return render(request, "search.html", principal, q=q, items=results)


@router.get("/files/{node_id}/detail")
def detail(
    request: Request,
    node_id: str,
    ok: str | None = None,
    error: str | None = None,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    node = get_node(db, node_id)
    access = authorize(db, principal, node, Permission.read)
    versions = list(
        db.scalars(
            select(NodeVersion)
            .where(NodeVersion.node_id == node.id)
            .order_by(NodeVersion.created_at.desc())
        )
    )
    can_manage = access.allows(Permission.owner)
    return render(
        request,
        "detail.html",
        principal,
        node=node,
        access=access,
        breadcrumbs=path_of(db, node),
        versions=versions,
        shares=shares_of(db, node) if can_manage else [],
        links=links_of(db, node) if can_manage else [],
        users=(
            db.scalars(select(User).where(User.id != node.owner_id, User.is_active.is_(True)))
            .all()
            if can_manage
            else []
        ),
        owner=db.get(User, node.owner_id),
        can_manage=can_manage,
        ok=ok,
        error=error,
    )


# --------------------------------------------------------------------------- #
# Descarga
# --------------------------------------------------------------------------- #


@router.get("/files/{node_id}/download")
def download(
    request: Request,
    node_id: str,
    principal: Principal = Depends(get_principal),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.read)
    require_action(principal, "download", config)

    allowed, retry_after = ratelimit.check("download", str(principal.ip), principal.ip, config)
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Demasiadas descargas seguidas.",
            headers={"Retry-After": str(retry_after)},
        )

    audit.record(db, "download", principal, target_type="node", target_id=node.id,
                 name=node.name, config=config)
    db.commit()
    return serve_blob(request, node, inline=False, config=config)


@router.get("/files/{node_id}/preview")
def preview(
    request: Request,
    node_id: str,
    principal: Principal = Depends(get_principal),
    db: DbSession = Depends(get_db),
):
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.read)
    return serve_blob(request, node, inline=True)


@router.get("/files/{node_id}/thumb")
def thumb(
    node_id: str,
    size: int = 256,
    principal: Principal = Depends(get_principal),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.read)

    if not node.blob_hash or not thumbnails.can_thumbnail(node.mime_type, node.size, config):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sin miniatura.")

    # Sólo se sirven los tamaños declarados, para que nadie pueda forzar la
    # generación de miles de variantes.
    size = min(config.thumbnails.sizes, key=lambda s: abs(s - size))
    path = thumbnails.generate(node.blob_hash, size, config)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sin miniatura.")
    return FileResponse(
        path,
        media_type="image/webp",
        headers={"Cache-Control": "private, max-age=604800"},
    )


# --------------------------------------------------------------------------- #
# Mutaciones
# --------------------------------------------------------------------------- #


@router.post("/files/upload")
def upload(
    request: Request,
    parent_id: str = Form(""),
    overwrite: bool = Form(False),
    relpath: str = Form(""),  # ruta relativa si se sube una carpeta
    files: list[UploadFile] = File(...),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    require_action(principal, "upload", config)
    user = principal.user
    assert user is not None

    parent = None
    owner = user
    if parent_id:
        parent = get_node(db, parent_id)
        authorize(db, principal, parent, Permission.write)
        if not parent.is_dir:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "El destino no es una carpeta.")
        # Al subir a una carpeta compartida, el fichero pertenece a su dueño
        # y consume su cuota, no la de quien lo sube.
        owner = db.get(User, parent.owner_id) or user

    saved, errors = 0, []
    for upload_file in files:
        if not upload_file.filename:
            continue
        try:
            node = files_service.save_upload(
                db, owner, files_service.ensure_folders(db, owner, parent, relpath),
                upload_file.filename, upload_file.file,
                overwrite=overwrite, config=config,
            )
            audit.record(db, "upload", principal, target_type="node", target_id=node.id,
                         name=node.name, size=node.size, config=config)
            saved += 1
        except StorageError as exc:
            db.rollback()
            errors.append(f"{upload_file.filename}: {exc}")
        finally:
            upload_file.file.close()

    db.commit()
    message = f"{saved} fichero(s) subidos." if saved else None
    return _back(parent, message, " ".join(errors) if errors else None)


@router.post("/files/mkdir")
def mkdir(
    name: str = Form(...),
    parent_id: str = Form(""),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
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
        files_service.create_folder(db, owner, parent, name)
        db.commit()
    except StorageError as exc:
        db.rollback()
        return _back(parent, error=str(exc))
    return _back(parent, "Carpeta creada.")


@router.post("/files/{node_id}/rename")
def rename(
    node_id: str,
    name: str = Form(...),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.write)
    parent = db.get(Node, node.parent_id) if node.parent_id else None
    try:
        files_service.rename(db, node, name)
        db.commit()
    except StorageError as exc:
        db.rollback()
        return _back(parent, error=str(exc))
    return _back(parent, "Renombrado.")


@router.post("/files/{node_id}/move")
def move(
    node_id: str,
    target_id: str = Form(""),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.write)

    target = None
    if target_id:
        target = get_node(db, target_id)
        authorize(db, principal, target, Permission.write)
        if target.owner_id != node.owner_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Mover entre cuentas no está soportado; usa copiar.",
            )

    try:
        files_service.move(db, node, target)
        db.commit()
    except StorageError as exc:
        db.rollback()
        return _back(target, error=str(exc))
    return _back(target, "Movido.")


@router.post("/files/{node_id}/copy")
def copy(
    node_id: str,
    target_id: str = Form(""),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    require_action(principal, "upload", config)
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.read)
    user = principal.user
    assert user is not None

    target = None
    if target_id:
        target = get_node(db, target_id)
        authorize(db, principal, target, Permission.write)

    try:
        files_service.copy(db, node, target, user, config)
        db.commit()
    except StorageError as exc:
        db.rollback()
        return _back(target, error=str(exc))
    return _back(target, "Copiado.")


@router.post("/files/{node_id}/trash")
def send_to_trash(
    node_id: str,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    require_action(principal, "delete", config)
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.write)
    parent = db.get(Node, node.parent_id) if node.parent_id else None

    files_service.trash(db, node, config)
    audit.record(db, "delete", principal, target_type="node", target_id=node.id,
                 name=node.name, permanent=False, config=config)
    db.commit()
    return _back(parent, "Enviado a la papelera.")


@router.post("/files/{node_id}/restore")
def restore(
    node_id: str,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.write)
    try:
        files_service.restore(db, node, get_config())
        db.commit()
    except StorageError as exc:
        db.rollback()
        return RedirectResponse(
            f"/trash?error={urllib.parse.quote(str(exc))}", status.HTTP_303_SEE_OTHER
        )
    return RedirectResponse("/trash?ok=Restaurado", status.HTTP_303_SEE_OTHER)


@router.post("/files/{node_id}/purge")
def purge(
    node_id: str,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    require_action(principal, "delete", config)
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.owner)

    audit.record(db, "delete", principal, target_type="node", target_id=node.id,
                 name=node.name, permanent=True, config=config)
    files_service.purge(db, node, config)
    db.commit()
    return RedirectResponse("/trash?ok=Eliminado+definitivamente", status.HTTP_303_SEE_OTHER)


@router.post("/trash/empty")
def empty_trash(
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    require_action(principal, "delete", config)
    user = principal.user
    assert user is not None
    count = files_service.empty_trash(db, user.id, config)
    audit.record(db, "delete", principal, target_type="trash", count=count, config=config)
    db.commit()
    return RedirectResponse(f"/trash?ok={count}+elemento(s)+eliminados", status.HTTP_303_SEE_OTHER)


@router.post("/files/{node_id}/star")
def toggle_star(
    node_id: str,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.write)
    node.starred = not node.starred
    db.commit()
    return _back(db.get(Node, node.parent_id) if node.parent_id else None)


@router.post("/files/{node_id}/versions/{version_id}/restore")
def restore_version(
    node_id: str,
    version_id: str,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.write)
    version = db.get(NodeVersion, version_id)
    if version is None or version.node_id != node.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Esa versión no existe.")

    owner = db.get(User, node.owner_id)
    assert owner is not None
    files_service.restore_version(db, node, version, owner, get_config())
    db.commit()
    return RedirectResponse(f"/files/{node.id}/detail?ok=Versión+restaurada",
                            status.HTTP_303_SEE_OTHER)
