"""Quién puede hacer qué.

Hay cinco maneras distintas de llegar a un fichero y todas confluyen aquí:

* siendo su propietario,
* teniéndolo compartido con tu cuenta (directamente o por una carpeta padre),
* porque está marcado como público,
* porque llegas por un enlace con token,
* porque entras desde una red de confianza y la política lo permite.

:func:`resolve` devuelve el permiso efectivo y de dónde sale, de forma que la
interfaz pueda explicárselo al usuario en lugar de limitarse a un 403.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .config import Config, get_config
from .models import Node, Permission, Role, Share, ShareLink, User, utcnow
from .netutils import IPAddress

#: Profundidad máxima al subir por el árbol. Evita quedarse colgado si una
#: incidencia de datos crea un ciclo padre-hijo.
MAX_DEPTH = 64

AccessSource = Literal["owner", "share", "public", "link", "network", "admin", "none"]

#: Acciones que ``network.require_login_for`` puede exigir con sesión iniciada.
GUARDED_ACTIONS = frozenset(
    {"upload", "delete", "share", "admin", "settings", "download"}
)


# --------------------------------------------------------------------------- #
# Identidad
# --------------------------------------------------------------------------- #


@dataclass
class Principal:
    """Quién está haciendo esta petición."""

    #: Cómo se ha identificado.
    kind: Literal["user", "anonymous", "network", "link", "token"] = "anonymous"
    user: User | None = None
    session_id: str | None = None
    ip: IPAddress | None = None
    #: La IP de origen cae dentro de ``network.trusted_networks``.
    trusted_network: bool = False
    #: Enlace con token por el que se ha entrado, si aplica.
    link: ShareLink | None = None
    #: Ámbitos del token de API, si se ha entrado con uno.
    scopes: list[str] = field(default_factory=list)
    #: Acceso degradado a sólo lectura (modo LAN readonly, enlaces de descarga).
    readonly: bool = False

    @property
    def is_authenticated(self) -> bool:
        return self.user is not None and self.kind in ("user", "token", "network")

    @property
    def is_admin(self) -> bool:
        return self.user is not None and self.user.role == Role.admin

    @property
    def user_id(self) -> str | None:
        return self.user.id if self.user else None

    @property
    def label(self) -> str:
        if self.user:
            return self.user.label
        if self.kind == "link":
            return "enlace compartido"
        if self.trusted_network:
            return "red local"
        return "anónimo"


# --------------------------------------------------------------------------- #
# Capacidades por perfil
# --------------------------------------------------------------------------- #


def capability(principal: Principal, name: str, config: Config | None = None) -> bool:
    """Evalúa una capacidad del perfil (``can_upload``, ``can_share_public``…).

    Orden de resolución: override individual del usuario > perfil del rol.
    """
    config = config or get_config()
    user = principal.user
    if user is None:
        return False

    override = (user.permission_overrides or {}).get(name)
    if isinstance(override, bool):
        return override

    return bool(getattr(config.role(user.role.value), name, False))


def action_allowed(
    principal: Principal, action: str, config: Config | None = None
) -> tuple[bool, str | None]:
    """Comprueba una acción global. Devuelve ``(permitida, motivo del no)``.

    Aquí se aplican las restricciones que no dependen de un fichero concreto:
    el perfil del rol, el modo de sólo lectura y la lista
    ``network.require_login_for``.
    """
    config = config or get_config()

    # Un acceso entrado por red de confianza sin login real no puede hacer las
    # acciones que la configuración reserve a usuarios identificados.
    if principal.kind == "network" and action in config.network.require_login_for:
        return False, "Esta acción requiere iniciar sesión, aunque estés en la red local."

    if principal.readonly and action in ("upload", "delete", "share", "settings", "admin"):
        return False, "Tu acceso actual es de sólo lectura."

    if action in ("admin", "settings") and not principal.is_authenticated:
        return False, "Necesitas iniciar sesión."

    checks = {
        "upload": "can_upload",
        "delete": "can_delete",
        "share": "can_share_internal",
        "share_public": "can_share_public",
        "admin": "can_manage_users",
        "settings": "can_edit_settings",
    }
    cap = checks.get(action)
    if cap and principal.user is not None and not capability(principal, cap, config):
        return False, "Tu perfil no permite esta acción."

    if action == "share" and not config.sharing.internal_enabled:
        return False, "La compartición entre usuarios está desactivada."
    if action == "share_public":
        if not config.sharing.public_links_enabled:
            return False, "Los enlaces públicos están desactivados."
        role = principal.user.role.value if principal.user else ""
        if role not in config.sharing.public_links_roles:
            return False, "Tu rol no puede crear enlaces públicos."

    return True, None


# --------------------------------------------------------------------------- #
# Árbol
# --------------------------------------------------------------------------- #


def chain(db: DbSession, node: Node) -> list[Node]:
    """El nodo y sus ancestros, del más cercano a la raíz."""
    result = [node]
    cursor = node
    seen = {node.id}
    for _ in range(MAX_DEPTH):
        if cursor.parent_id is None:
            break
        parent = db.get(Node, cursor.parent_id)
        if parent is None or parent.id in seen:
            break
        seen.add(parent.id)
        result.append(parent)
        cursor = parent
    return result


def path_of(db: DbSession, node: Node) -> list[Node]:
    """Ruta desde la raíz hasta el nodo, para las migas de pan."""
    return list(reversed(chain(db, node)))


def is_descendant(db: DbSession, node: Node, ancestor_id: str) -> bool:
    return any(n.id == ancestor_id for n in chain(db, node))


# --------------------------------------------------------------------------- #
# Resolución de acceso
# --------------------------------------------------------------------------- #


@dataclass
class Access:
    permission: Permission | None
    source: AccessSource = "none"
    #: Nodo del que cuelga el permiso (puede ser un ancestro).
    via: Node | None = None

    @property
    def granted(self) -> bool:
        return self.permission is not None

    def allows(self, needed: Permission) -> bool:
        return self.permission is not None and self.permission.covers(needed)


def _share_for(
    db: DbSession, user_id: str, node_ids: list[str], now: datetime
) -> tuple[Permission, str] | None:
    """Mejor permiso compartido con un usuario sobre una cadena de nodos."""
    rows = db.scalars(
        select(Share).where(
            Share.user_id == user_id,
            Share.node_id.in_(node_ids),
        )
    ).all()
    best: tuple[Permission, str] | None = None
    for share in rows:
        if share.expires_at is not None and share.expires_at <= now:
            continue
        if best is None or share.permission.rank > best[0].rank:
            best = (share.permission, share.node_id)
    return best


def resolve(
    db: DbSession,
    principal: Principal,
    node: Node,
    config: Config | None = None,
) -> Access:
    """Permiso efectivo de ``principal`` sobre ``node``."""
    config = config or get_config()
    now = utcnow()
    lineage = chain(db, node)
    lineage_ids = [n.id for n in lineage]
    by_id = {n.id: n for n in lineage}

    # Un elemento en la papelera sólo lo ve su dueño (y los administradores),
    # aunque estuviera compartido o fuera público antes de borrarse.
    trashed = any(n.deleted_at is not None for n in lineage)

    # 1. Propietario.
    if principal.user is not None and node.owner_id == principal.user.id:
        return Access(Permission.owner, "owner", node)

    # 2. Administrador: acceso total, también a la papelera ajena.
    if principal.is_admin:
        return Access(Permission.owner, "admin", node)

    if trashed:
        return Access(None, "none")

    # 3. Enlace con token.
    if principal.link is not None and principal.link.node_id in lineage_ids:
        link = principal.link
        target = by_id.get(link.node_id)
        # Un enlace a una carpeta sin listado sólo sirve para esa carpeta.
        if not link.allow_listing and node.id != link.node_id:
            return Access(None, "none")
        perm = Permission.write if link.mode.value in ("upload", "both") else Permission.read
        return Access(perm, "link", target)

    # 4. Compartido con la cuenta, directamente o por herencia.
    if principal.user is not None:
        shared = _share_for(db, principal.user.id, lineage_ids, now)
        if shared is not None:
            permission, via_id = shared
            return Access(permission, "share", by_id.get(via_id))

    # 5. Público: cualquiera, con o sin cuenta.
    for candidate in lineage:
        if candidate.is_public:
            return Access(Permission.read, "public", candidate)

    # 6. Red de confianza.
    if principal.trusted_network and config.network.trusted_mode != "none":
        access = _network_access(db, principal, lineage, by_id, now, config)
        if access is not None:
            return access

    return Access(None, "none")


def _network_access(
    db: DbSession,
    principal: Principal,
    lineage: list[Node],
    by_id: dict[str, Node],
    now: datetime,
    config: Config,
) -> Access | None:
    """Acceso concedido por estar en una red de confianza."""
    scope = config.network.trusted_scope
    trusted_user_id = _trusted_user_id(db, config)

    if scope == "public":
        # Lo público ya se ha resuelto antes; la red no añade nada.
        return None

    if scope == "shared":
        for candidate in lineage:
            if candidate.lan_visible:
                return Access(Permission.read, "network", candidate)
        if trusted_user_id:
            shared = _share_for(db, trusted_user_id, [n.id for n in lineage], now)
            if shared is not None:
                permission, via_id = shared
                if config.network.trusted_mode == "readonly":
                    permission = Permission.read
                return Access(permission, "network", by_id.get(via_id))
        return None

    if scope == "trusted_user" and trusted_user_id:
        if lineage[-1].owner_id == trusted_user_id or any(
            n.owner_id == trusted_user_id for n in lineage
        ):
            permission = (
                Permission.read
                if config.network.trusted_mode == "readonly"
                else Permission.owner
            )
            return Access(permission, "network", lineage[-1])

    return None


def _trusted_user_id(db: DbSession, config: Config) -> str | None:
    if not config.network.trusted_user:
        return None
    user = db.scalar(select(User).where(User.username == config.network.trusted_user))
    return user.id if user is not None else None


# --------------------------------------------------------------------------- #
# Estado de un enlace
# --------------------------------------------------------------------------- #


def link_is_usable(link: ShareLink, ip: IPAddress | None = None) -> tuple[bool, str]:
    """Comprueba caducidad, revocación, límite de descargas y red permitida."""
    from .netutils import matches_any

    if link.revoked:
        return False, "Este enlace ha sido revocado."
    if link.expires_at is not None and link.expires_at <= utcnow():
        return False, "Este enlace ha caducado."
    if link.download_limit is not None and link.download_count >= link.download_limit:
        return False, "Este enlace ha alcanzado su límite de descargas."
    if link.allowed_networks and not matches_any(ip, link.allowed_networks):
        return False, "Este enlace no está disponible desde tu red."
    return True, ""
