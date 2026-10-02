"""Acceso sin cuenta: ficheros públicos, red local y enlaces con token."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import audit, files_service, ratelimit, shares_service
from ..auth import get_principal
from ..config import get_config
from ..database import get_db
from ..models import LinkMode, Node, Permission, ShareLink, User
from ..permissions import Principal, link_is_usable, path_of, resolve
from ..storage import StorageError
from ..templating import render
from .files_routes import authorize, get_node, serve_blob

router = APIRouter(tags=["public"])

UNLOCKED_COOKIE = "drive_links"


# --------------------------------------------------------------------------- #
# Enlaces desbloqueados por contraseña
# --------------------------------------------------------------------------- #


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_config().security.secret_key, salt="share-links")


def _unlocked(request: Request) -> set[str]:
    raw = request.cookies.get(UNLOCKED_COOKIE)
    if not raw:
        return set()
    try:
        return set(_serializer().loads(raw))
    except BadSignature:
        return set()


def _remember_unlocked(response: Response, request: Request, token: str) -> None:
    tokens = _unlocked(request)
    tokens.add(token)
    # Se limita el tamaño para que la cookie no crezca indefinidamente.
    payload = _serializer().dumps(sorted(tokens)[-30:])
    config = get_config()
    response.set_cookie(
        UNLOCKED_COOKIE,
        payload,
        max_age=86400,
        httponly=True,
        secure=config.security.session.cookie_secure,
        samesite=config.security.session.cookie_samesite,
        path="/",
    )


# --------------------------------------------------------------------------- #
# Vista pública general
# --------------------------------------------------------------------------- #


@router.get("/browse")
def browse_public(
    request: Request,
    node_id: str | None = None,
    principal: Principal = Depends(get_principal),
    db: DbSession = Depends(get_db),
):
    """Índice de lo accesible sin iniciar sesión desde esta red."""
    config = get_config()

    if node_id:
        node = get_node(db, node_id)
        access = resolve(db, principal, node, config)
        if not access.granted:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese elemento.")
        if not node.is_dir:
            return RedirectResponse(f"/p/{node.id}", status.HTTP_303_SEE_OTHER)
        items = [
            child
            for child in files_service.children_of(db, node.owner_id, node.id)
            if resolve(db, principal, child, config).granted
        ]
        return render(
            request, "public_browse.html", principal,
            current=node, items=items, breadcrumbs=path_of(db, node),
        )

    # Raíz: lo marcado como público y, si la red es de confianza, lo que la
    # política de red abra además.
    roots = list(db.scalars(select(Node).where(Node.is_public.is_(True), Node.deleted_at.is_(None))))
    if principal.trusted_network and config.network.trusted_mode != "none":
        roots += [
            n
            for n in db.scalars(
                select(Node).where(Node.lan_visible.is_(True), Node.deleted_at.is_(None))
            )
            if n not in roots
        ]
        if config.network.trusted_scope == "trusted_user" and config.network.trusted_user:
            owner = db.scalar(
                select(User).where(User.username == config.network.trusted_user)
            )
            if owner is not None:
                roots += files_service.children_of(db, owner.id, None)

    # Se ocultan los nodos que ya cuelgan de otro que también se muestra.
    shown = {n.id for n in roots}
    roots = [n for n in roots if n.parent_id not in shown]

    return render(
        request, "public_browse.html", principal,
        current=None, items=roots, breadcrumbs=[],
        trusted=principal.trusted_network,
    )


@router.get("/p/{node_id}")
def public_node(
    request: Request,
    node_id: str,
    principal: Principal = Depends(get_principal),
    db: DbSession = Depends(get_db),
):
    node = get_node(db, node_id)
    access = resolve(db, principal, node, get_config())
    if not access.granted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese elemento.")
    if node.is_dir:
        return RedirectResponse(f"/browse?node_id={node.id}", status.HTTP_303_SEE_OTHER)
    return render(
        request, "public_file.html", principal,
        node=node, access=access, download_url=f"/p/{node.id}/download",
    )


@router.get("/p/{node_id}/download")
def public_download(
    request: Request,
    node_id: str,
    principal: Principal = Depends(get_principal),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.read)

    allowed, retry_after = ratelimit.check("download", str(principal.ip), principal.ip, config)
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Demasiadas descargas seguidas.",
            headers={"Retry-After": str(retry_after)},
        )

    audit.record(db, "download", principal, target_type="node", target_id=node.id,
                 name=node.name, via="public", config=config)
    db.commit()
    return serve_blob(request, node, inline=False, config=config)


# --------------------------------------------------------------------------- #
# Enlaces con token
# --------------------------------------------------------------------------- #


def _load_link(db: DbSession, token: str, principal: Principal) -> ShareLink:
    link = db.scalar(select(ShareLink).where(ShareLink.token == token))
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Este enlace no existe.")
    usable, reason = link_is_usable(link, principal.ip)
    if not usable:
        raise HTTPException(status.HTTP_410_GONE, reason)
    node = db.get(Node, link.node_id)
    if node is None or node.deleted_at is not None:
        raise HTTPException(status.HTTP_410_GONE, "El contenido ya no está disponible.")
    return link


def _link_principal(base: Principal, link: ShareLink) -> Principal:
    return Principal(
        kind="link",
        ip=base.ip,
        trusted_network=base.trusted_network,
        link=link,
        readonly=link.mode == LinkMode.download,
    )


@router.get("/s/{token}")
def open_link(
    request: Request,
    token: str,
    node_id: str | None = None,
    ok: str | None = None,
    error: str | None = None,
    principal: Principal = Depends(get_principal),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    link = _load_link(db, token, principal)

    if link.password_hash and token not in _unlocked(request):
        return render(request, "link_password.html", None, token=token)

    root = db.get(Node, link.node_id)
    assert root is not None
    node = root
    if node_id:
        node = get_node(db, node_id)
        access = resolve(db, _link_principal(principal, link), node, config)
        if not access.granted:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese elemento.")

    items = []
    if node.is_dir and link.allow_listing:
        items = files_service.children_of(db, node.owner_id, node.id)

    audit.record(db, "share_access", principal, target_type="link", target_id=link.id,
                 node=node.id, config=config)
    db.commit()

    return render(
        request, "link.html", None,
        link=link, node=node, root=root, items=items,
        can_upload=link.mode in (LinkMode.upload, LinkMode.both),
        can_download=link.mode in (LinkMode.download, LinkMode.both),
        breadcrumbs=[n for n in path_of(db, node) if n.id != root.parent_id],
        ok=ok, error=error,
    )


@router.post("/s/{token}")
def unlock_link(
    request: Request,
    token: str,
    password: str = Form(...),
    principal: Principal = Depends(get_principal),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    link = _load_link(db, token, principal)

    allowed, retry_after = ratelimit.check("login", f"link:{token}", principal.ip, config)
    if not allowed:
        return render(
            request, "link_password.html", None,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            token=token, error=f"Demasiados intentos. Espera {retry_after} segundos.",
        )

    if not shares_service.check_link_password(link, password):
        return render(
            request, "link_password.html", None,
            status_code=status.HTTP_401_UNAUTHORIZED,
            token=token, error="Contraseña incorrecta.",
        )

    response = RedirectResponse(f"/s/{token}", status.HTTP_303_SEE_OTHER)
    _remember_unlocked(response, request, token)
    return response


@router.get("/s/{token}/download/{node_id}")
def link_download(
    request: Request,
    token: str,
    node_id: str,
    principal: Principal = Depends(get_principal),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    link = _load_link(db, token, principal)
    if link.password_hash and token not in _unlocked(request):
        return RedirectResponse(f"/s/{token}", status.HTTP_303_SEE_OTHER)
    if link.mode == LinkMode.upload:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Este enlace sólo admite subidas.")

    node = get_node(db, node_id)
    link_principal = _link_principal(principal, link)
    access = resolve(db, link_principal, node, config)
    if not access.granted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No existe ese elemento.")

    shares_service.register_download(db, link)
    audit.record(db, "download", link_principal, target_type="node", target_id=node.id,
                 via="link", token=token, config=config)
    db.commit()
    return serve_blob(request, node, inline=False, config=config)


@router.post("/s/{token}/upload")
def link_upload(
    request: Request,
    token: str,
    node_id: str = Form(""),
    relpath: str = Form(""),  # ruta relativa si se sube una carpeta
    files: list[UploadFile] = File(...),
    principal: Principal = Depends(get_principal),
    db: DbSession = Depends(get_db),
):
    """Buzón: un visitante deja ficheros sin necesidad de cuenta."""
    config = get_config()
    link = _load_link(db, token, principal)
    if link.password_hash and token not in _unlocked(request):
        return RedirectResponse(f"/s/{token}", status.HTTP_303_SEE_OTHER)
    if link.mode not in (LinkMode.upload, LinkMode.both):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Este enlace no admite subidas.")

    target = get_node(db, node_id) if node_id else db.get(Node, link.node_id)
    if target is None or not target.is_dir:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "El destino no es una carpeta.")

    access = resolve(db, _link_principal(principal, link), target, config)
    if not access.allows(Permission.write):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No puedes subir aquí.")

    owner = db.get(User, target.owner_id)
    if owner is None:
        raise HTTPException(status.HTTP_410_GONE, "El destino ya no existe.")

    saved, errors = 0, []
    for upload in files:
        if not upload.filename:
            continue
        try:
            node = files_service.save_upload(
                db, owner, files_service.ensure_folders(db, owner, target, relpath),
                upload.filename, upload.file, config=config,
            )
            audit.record(db, "upload", principal, target_type="node", target_id=node.id,
                         via="link", token=token, name=node.name, config=config)
            saved += 1
        except StorageError as exc:
            db.rollback()
            errors.append(f"{upload.filename}: {exc}")
        finally:
            upload.file.close()
    db.commit()

    params = f"?ok={saved}+fichero(s)+recibidos" if saved else ""
    if errors:
        params = "?error=" + "+".join(" ".join(errors).split())
    return RedirectResponse(f"/s/{token}{params}", status.HTTP_303_SEE_OTHER)
