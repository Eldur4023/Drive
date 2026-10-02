"""Compartición interna y enlaces públicos."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .config import Config, get_config
from .models import LinkMode, Node, Permission, Share, ShareLink, User, utcnow
from .security import generate_token, hash_password, verify_password
from .storage import StorageError


class SharingError(StorageError):
    pass


# --------------------------------------------------------------------------- #
# Compartición con usuarios
# --------------------------------------------------------------------------- #


def share_with_user(
    db: DbSession,
    node: Node,
    target: User,
    permission: Permission,
    created_by: User,
    *,
    expires_in: int | None = None,
    message: str | None = None,
    config: Config | None = None,
) -> Share:
    config = config or get_config()
    if not config.sharing.internal_enabled:
        raise SharingError("La compartición entre usuarios está desactivada.")
    if target.id == node.owner_id:
        raise SharingError("Ese usuario ya es el propietario.")
    if not target.is_active:
        raise SharingError("Esa cuenta está desactivada.")

    existing = db.scalar(
        select(Share).where(Share.node_id == node.id, Share.user_id == target.id)
    )
    share = existing or Share(node_id=node.id, user_id=target.id, created_by=created_by.id)
    share.permission = permission
    share.message = message
    share.expires_at = (
        utcnow() + timedelta(seconds=expires_in) if expires_in else None
    )
    db.add(share)
    db.flush()
    return share


def unshare(db: DbSession, share: Share) -> None:
    db.delete(share)
    db.flush()


def shares_of(db: DbSession, node: Node) -> list[Share]:
    return list(db.scalars(select(Share).where(Share.node_id == node.id)))


# --------------------------------------------------------------------------- #
# Enlaces públicos
# --------------------------------------------------------------------------- #


def _resolve_expiry(expires_in: int | None, config: Config) -> int | None:
    """Aplica el valor por defecto y el tope máximo de caducidad."""
    if expires_in is None:
        expires_in = config.sharing.default_link_expiry
    if expires_in is not None and config.sharing.max_link_expiry:
        expires_in = min(expires_in, config.sharing.max_link_expiry)
    return expires_in


def create_link(
    db: DbSession,
    node: Node,
    created_by: User,
    *,
    mode: LinkMode = LinkMode.download,
    password: str | None = None,
    expires_in: int | None = None,
    download_limit: int | None = None,
    allow_listing: bool = True,
    allowed_networks: list[str] | None = None,
    note: str | None = None,
    config: Config | None = None,
) -> ShareLink:
    config = config or get_config()
    if not config.sharing.public_links_enabled:
        raise SharingError("Los enlaces públicos están desactivados.")
    if created_by.role.value not in config.sharing.public_links_roles:
        raise SharingError("Tu rol no puede crear enlaces públicos.")
    if mode in (LinkMode.upload, LinkMode.both):
        if not config.sharing.allow_upload_links:
            raise SharingError("Los enlaces de subida están desactivados.")
        if not node.is_dir:
            raise SharingError("Un enlace de subida debe apuntar a una carpeta.")
    if config.sharing.require_password_on_public_links and not password:
        raise SharingError("La política exige poner contraseña a los enlaces públicos.")

    expires_in = _resolve_expiry(expires_in, config)
    if download_limit is None:
        download_limit = config.sharing.default_download_limit

    link = ShareLink(
        token=generate_token(config.sharing.token_length),
        node_id=node.id,
        created_by=created_by.id,
        mode=mode,
        password_hash=hash_password(password, config) if password else None,
        expires_at=utcnow() + timedelta(seconds=expires_in) if expires_in else None,
        download_limit=download_limit,
        allow_listing=allow_listing,
        allowed_networks=allowed_networks or [],
        note=note,
    )
    db.add(link)
    db.flush()
    return link


def link_url(link: ShareLink, config: Config | None = None) -> str:
    config = config or get_config()
    return f"{config.app.base_url}/s/{link.token}"


def check_link_password(link: ShareLink, password: str) -> bool:
    if not link.password_hash:
        return True
    return verify_password(password or "", link.password_hash)


def links_of(db: DbSession, node: Node) -> list[ShareLink]:
    return list(
        db.scalars(
            select(ShareLink)
            .where(ShareLink.node_id == node.id, ShareLink.revoked.is_(False))
            .order_by(ShareLink.created_at.desc())
        )
    )


def links_by_user(db: DbSession, user_id: str) -> list[ShareLink]:
    return list(
        db.scalars(
            select(ShareLink)
            .where(ShareLink.created_by == user_id, ShareLink.revoked.is_(False))
            .order_by(ShareLink.created_at.desc())
        )
    )


def revoke_link(db: DbSession, link: ShareLink) -> None:
    link.revoked = True
    db.flush()


def register_download(db: DbSession, link: ShareLink) -> None:
    link.download_count += 1
    db.flush()
