"""Modelo de datos.

Las marcas de tiempo se guardan siempre en UTC y sin zona horaria, para que
SQLite y PostgreSQL se comporten igual; la conversión a la zona del usuario
ocurre sólo al renderizar.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Enumeraciones
# --------------------------------------------------------------------------- #


class Role(str, enum.Enum):
    admin = "admin"
    user = "user"
    guest = "guest"


class Permission(str, enum.Enum):
    """Nivel de acceso sobre un nodo, de menor a mayor."""

    read = "read"
    write = "write"
    owner = "owner"

    @property
    def rank(self) -> int:
        return {"read": 1, "write": 2, "owner": 3}[self.value]

    def covers(self, other: "Permission") -> bool:
        return self.rank >= other.rank


class LinkMode(str, enum.Enum):
    #: El visitante ve y descarga.
    download = "download"
    #: El visitante sólo puede depositar ficheros (buzón).
    upload = "upload"
    #: Ambas cosas.
    both = "both"


# --------------------------------------------------------------------------- #
# Cuentas
# --------------------------------------------------------------------------- #


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False), default=Role.user)

    display_name: Mapped[str | None] = mapped_column(String(128))
    #: Perfil libre editable por el propio usuario.
    bio: Mapped[str | None] = mapped_column(Text)
    avatar_node_id: Mapped[str | None] = mapped_column(String(32))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Cuentas pendientes de aprobación por un administrador.
    is_approved: Mapped[bool] = mapped_column(Boolean, default=True)

    #: Overrides del perfil de rol. ``None`` = hereda del rol.
    quota_override: Mapped[int | None] = mapped_column(BigInteger)
    max_upload_override: Mapped[int | None] = mapped_column(BigInteger)
    #: Overrides booleanos de capacidades, por nombre (can_upload, ...).
    permission_overrides: Mapped[dict] = mapped_column(JSON, default=dict)

    #: Bytes ocupados, mantenido de forma incremental para no recorrer el árbol.
    storage_used: Mapped[int] = mapped_column(BigInteger, default=0)

    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Preferencias de interfaz del usuario (tema, idioma, orden, vista).
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    password_changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)

    nodes: Mapped[list["Node"]] = relationship(
        back_populates="owner", foreign_keys="Node.owner_id"
    )

    @property
    def label(self) -> str:
        return self.display_name or self.username


class Session(Base):
    """Sesión de navegador. El identificador de cookie es el hash de ``id``."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    absolute_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Sesión creada automáticamente por estar en una red de confianza.
    from_trusted_network: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Sesión a medio autenticar: falta el segundo factor.
    pending_totp: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship()


class ApiToken(Base):
    """Token para clientes no interactivos (scripts, rclone, backups)."""

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    #: Lista de ámbitos: read, write, share, admin.
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship()


class LoginAttempt(Base):
    """Intentos de acceso, usados para el bloqueo por fuerza bruta."""

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: ``user:<username>`` o ``ip:<addr>``.
    key: Mapped[str] = mapped_column(String(128), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------- #
# Contenido
# --------------------------------------------------------------------------- #


class Blob(Base):
    """Contenido físico de un fichero, referenciado por hash."""

    __tablename__ = "blobs"

    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger)
    refcount: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Node(Base):
    """Un fichero o una carpeta dentro del árbol de un usuario."""

    __tablename__ = "nodes"
    __table_args__ = (
        # Nombres únicos entre hermanos vivos; la papelera queda fuera porque
        # dos ficheros borrados pueden coexistir con el mismo nombre.
        Index("ix_nodes_parent_name", "parent_id", "name"),
        Index("ix_nodes_owner_deleted", "owner_id", "deleted_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    is_dir: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Sólo para ficheros.
    blob_hash: Mapped[str | None] = mapped_column(ForeignKey("blobs.hash"))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    mime_type: Mapped[str | None] = mapped_column(String(128))

    #: Accesible por cualquiera sin iniciar sesión, en /p/<id>.
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    #: Visible sin login desde redes de confianza, aunque no sea público.
    lan_visible: Mapped[bool] = mapped_column(Boolean, default=False)

    starred: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
    #: Marca de papelera. ``None`` = vivo.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    #: Padre original, para poder restaurar desde la papelera.
    trashed_from_id: Mapped[str | None] = mapped_column(String(32))

    owner: Mapped[User] = relationship(back_populates="nodes", foreign_keys=[owner_id])
    parent: Mapped["Node | None"] = relationship(
        remote_side=[id], back_populates="children"
    )
    children: Mapped[list["Node"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )

    @property
    def is_trashed(self) -> bool:
        return self.deleted_at is not None

    @property
    def extension(self) -> str:
        _, dot, ext = self.name.rpartition(".")
        return f".{ext.lower()}" if dot else ""


class NodeVersion(Base):
    """Versión anterior de un fichero, conservada al sobreescribirlo."""

    __tablename__ = "node_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    blob_hash: Mapped[str] = mapped_column(ForeignKey("blobs.hash"))
    size: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    created_by: Mapped[str | None] = mapped_column(String(32))


# --------------------------------------------------------------------------- #
# Compartición
# --------------------------------------------------------------------------- #


class Share(Base):
    """Compartición con un usuario concreto de la instancia."""

    __tablename__ = "shares"
    __table_args__ = (UniqueConstraint("node_id", "user_id", name="uq_share_node_user"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    permission: Mapped[Permission] = mapped_column(
        Enum(Permission, native_enum=False), default=Permission.read
    )
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    message: Mapped[str | None] = mapped_column(Text)

    node: Mapped[Node] = relationship()
    user: Mapped[User] = relationship()


class ShareLink(Base):
    """Enlace público con token, con o sin contraseña."""

    __tablename__ = "share_links"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(32))
    mode: Mapped[LinkMode] = mapped_column(
        Enum(LinkMode, native_enum=False), default=LinkMode.download
    )
    password_hash: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    download_limit: Mapped[int | None] = mapped_column(Integer)
    download_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Permite listar el contenido si el nodo es una carpeta.
    allow_listing: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Restringe el enlace a estas redes (CIDR). Vacío = sin restricción.
    allowed_networks: Mapped[list] = mapped_column(JSON, default=list)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text)

    node: Mapped[Node] = relationship()


# --------------------------------------------------------------------------- #
# Auditoría y ajustes
# --------------------------------------------------------------------------- #


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    #: Nombre mostrado en el registro cuando no hay usuario (enlaces, LAN).
    actor: Mapped[str | None] = mapped_column(String(128))
    ip: Mapped[str | None] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class SettingOverride(Base):
    """Ajuste modificado desde el panel, con prioridad sobre el YAML."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(32))
