"""Gestión de comparticiones y enlaces."""

from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import audit, shares_service
from ..auth import require_action, require_user
from ..config import get_config
from ..database import get_db
from ..models import LinkMode, Node, Permission, Share, ShareLink, User
from ..permissions import Principal
from ..shares_service import SharingError
from ..templating import render
from .files_routes import authorize, get_node

router = APIRouter(tags=["shares"])


def _to_detail(node_id: str, ok: str | None = None, error: str | None = None):
    target = f"/files/{node_id}/detail"
    params = {k: v for k, v in (("ok", ok), ("error", error)) if v}
    if params:
        target += "?" + urllib.parse.urlencode(params)
    return RedirectResponse(target, status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- #
# Compartir con usuarios
# --------------------------------------------------------------------------- #


@router.post("/files/{node_id}/share")
def share_with_user(
    node_id: str,
    username: str = Form(...),
    permission: str = Form("read"),
    expires_days: int = Form(0),
    message: str = Form(""),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    require_action(principal, "share", config)
    node = get_node(db, node_id)
    access = authorize(db, principal, node, Permission.owner)

    if access.source == "share" and not config.sharing.allow_reshare:
        return _to_detail(node_id, error="No puedes volver a compartir algo compartido contigo.")

    target = db.scalar(select(User).where(User.username == username.strip()))
    if target is None:
        return _to_detail(node_id, error="No existe ese usuario.")

    try:
        shares_service.share_with_user(
            db, node, target,
            Permission(permission),
            principal.user,  # type: ignore[arg-type]
            expires_in=expires_days * 86400 if expires_days else None,
            message=message.strip() or None,
            config=config,
        )
    except (SharingError, ValueError) as exc:
        db.rollback()
        return _to_detail(node_id, error=str(exc))

    audit.record(db, "share_create", principal, target_type="node", target_id=node.id,
                 kind="user", to=target.username, permission=permission, config=config)
    db.commit()
    return _to_detail(node_id, ok=f"Compartido con {target.username}.")


@router.post("/shares/{share_id}/delete")
def remove_share(
    share_id: str,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    share = db.get(Share, share_id)
    if share is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Esa compartición no existe.")
    node = get_node(db, share.node_id)
    authorize(db, principal, node, Permission.owner)

    shares_service.unshare(db, share)
    db.commit()
    return _to_detail(node.id, ok="Compartición retirada.")


# --------------------------------------------------------------------------- #
# Enlaces públicos
# --------------------------------------------------------------------------- #


@router.post("/files/{node_id}/link")
def create_link(
    node_id: str,
    mode: str = Form("download"),
    password: str = Form(""),
    expires_days: int = Form(0),
    download_limit: int = Form(0),
    allow_listing: bool = Form(False),
    allowed_networks: str = Form(""),
    note: str = Form(""),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    require_action(principal, "share_public", config)
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.owner)

    networks = [n.strip() for n in allowed_networks.split(",") if n.strip()]
    try:
        link = shares_service.create_link(
            db, node, principal.user,  # type: ignore[arg-type]
            mode=LinkMode(mode),
            password=password or None,
            expires_in=expires_days * 86400 if expires_days else None,
            download_limit=download_limit or None,
            allow_listing=allow_listing,
            allowed_networks=networks,
            note=note.strip() or None,
            config=config,
        )
    except (SharingError, ValueError) as exc:
        db.rollback()
        return _to_detail(node_id, error=str(exc))

    audit.record(db, "share_create", principal, target_type="node", target_id=node.id,
                 kind="link", token=link.token, config=config)
    db.commit()
    return _to_detail(node_id, ok=f"Enlace creado: {shares_service.link_url(link, config)}")


@router.post("/links/{link_id}/revoke")
def revoke_link(
    link_id: str,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    link = db.get(ShareLink, link_id)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ese enlace no existe.")
    node = get_node(db, link.node_id)
    authorize(db, principal, node, Permission.owner)

    shares_service.revoke_link(db, link)
    db.commit()
    return _to_detail(node.id, ok="Enlace revocado.")


@router.get("/links")
def my_links(
    request: Request,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    """Todos los enlaces creados por el usuario, para poder auditarlos."""
    config = get_config()
    links = shares_service.links_by_user(db, principal.user.id)  # type: ignore[union-attr]
    return render(
        request,
        "links.html",
        principal,
        links=links,
        nodes={link.id: db.get(Node, link.node_id) for link in links},
        base_url=config.app.base_url,
    )


# --------------------------------------------------------------------------- #
# Visibilidad directa (público / red local)
# --------------------------------------------------------------------------- #


@router.post("/files/{node_id}/visibility")
def set_visibility(
    node_id: str,
    is_public: bool = Form(False),
    lan_visible: bool = Form(False),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    node = get_node(db, node_id)
    authorize(db, principal, node, Permission.owner)

    if is_public and not node.is_public:
        require_action(principal, "share_public", config)

    node.is_public = is_public
    node.lan_visible = lan_visible
    audit.record(db, "settings_change", principal, target_type="node", target_id=node.id,
                 is_public=is_public, lan_visible=lan_visible, config=config)
    db.commit()
    return _to_detail(node_id, ok="Visibilidad actualizada.")
