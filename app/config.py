"""Carga y validación de la configuración.

La configuración se resuelve en tres capas, de menor a mayor prioridad:

1. Los defaults declarados en los modelos de este módulo.
2. El fichero YAML (``config/config.yaml`` salvo que se indique otro con
   ``DRIVE_CONFIG``).
3. Variables de entorno con prefijo ``DRIVE_`` y ``__`` como separador de nivel.

Los tamaños admiten sufijos (``2GB``) y las duraciones también (``30d``); ambos
se normalizan a enteros —bytes y segundos respectivamente— en el momento de
validar, de modo que el resto de la aplicación sólo ve números.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
)

# --------------------------------------------------------------------------- #
# Conversores
# --------------------------------------------------------------------------- #

_SIZE_UNITS = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}
_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([a-z]*)\s*$", re.IGNORECASE)

_TIME_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_TIME_RE = re.compile(r"^\s*([\d.]+)\s*([a-z]*)\s*$", re.IGNORECASE)


def parse_size(value: Any) -> Any:
    """``"2GB"`` -> ``2147483648``. ``None`` y ``0`` significan "sin límite"."""
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, str):
        match = _SIZE_RE.match(value)
        if not match:
            raise ValueError(f"tamaño no reconocido: {value!r}")
        number, unit = match.groups()
        unit = unit.lower() or "b"
        if unit not in _SIZE_UNITS:
            raise ValueError(f"unidad de tamaño desconocida: {unit!r}")
        return int(float(number) * _SIZE_UNITS[unit])
    raise ValueError(f"tamaño no reconocido: {value!r}")


def parse_duration(value: Any) -> Any:
    """``"30d"`` -> ``2592000`` segundos. ``None`` significa "sin caducidad"."""
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, str):
        match = _TIME_RE.match(value)
        if not match:
            raise ValueError(f"duración no reconocida: {value!r}")
        number, unit = match.groups()
        unit = unit.lower() or "s"
        if unit not in _TIME_UNITS:
            raise ValueError(f"unidad de duración desconocida: {unit!r}")
        return int(float(number) * _TIME_UNITS[unit])
    raise ValueError(f"duración no reconocida: {value!r}")


def _normalise_limit(value: int | None) -> int | None:
    """Trata 0 como "sin límite" para que ``null`` y ``0`` sean equivalentes."""
    return None if not value else value


Size = Annotated[int | None, BeforeValidator(parse_size)]
Seconds = Annotated[int | None, BeforeValidator(parse_duration)]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Secciones
# --------------------------------------------------------------------------- #


class AppConfig(_Base):
    name: str = "Drive"
    base_url: str = "http://localhost:8000"
    language: Literal["es", "en"] = "es"
    timezone: str = "Europe/Madrid"

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")


class ServerConfig(_Base):
    host: str = "127.0.0.1"
    port: int = 8000
    workers: int = 2
    behind_proxy: bool = False
    trusted_proxies: list[str] = ["127.0.0.1/32", "::1/128"]


class DatabaseConfig(_Base):
    url: str = "sqlite:///./data/drive.db"
    echo: bool = False
    pool_size: int = 5
    max_overflow: int = 10


class StorageConfig(_Base):
    root: str = "./data/blobs"
    max_upload_size: Size = 2 * 1024**3
    default_quota: Size = 10 * 1024**3
    deduplicate: bool = True
    hash_algorithm: str = "sha256"
    blocked_extensions: list[str] = []
    allowed_extensions: list[str] = []
    blocked_mime_types: list[str] = []

    @field_validator("blocked_extensions", "allowed_extensions")
    @classmethod
    def _normalise_ext(cls, v: list[str]) -> list[str]:
        return [e.lower() if e.startswith(".") else f".{e.lower()}" for e in v]

    @field_validator("max_upload_size", "default_quota")
    @classmethod
    def _no_limit_is_none(cls, v: int | None) -> int | None:
        return _normalise_limit(v)


class TrashConfig(_Base):
    enabled: bool = True
    retention: Seconds = 30 * 86400
    counts_against_quota: bool = True


class VersionsConfig(_Base):
    enabled: bool = True
    max_per_file: int = 10
    retention: Seconds = 90 * 86400


class ThumbnailsConfig(_Base):
    enabled: bool = True
    sizes: list[int] = [128, 512]
    max_source_size: Size = 25 * 1024**2


class AuthConfig(_Base):
    registration_enabled: bool = False
    allowed_email_domains: list[str] = []
    require_admin_approval: bool = True
    default_role: Literal["admin", "user", "guest"] = "user"
    allow_email_login: bool = True


class PasswordPolicy(_Base):
    min_length: int = 10
    require_uppercase: bool = True
    require_lowercase: bool = True
    require_digit: bool = True
    require_symbol: bool = False
    reject_username_in_password: bool = True


class SessionConfig(_Base):
    lifetime: Seconds = 7 * 86400
    absolute_lifetime: Seconds = 30 * 86400
    cookie_name: str = "drive_session"
    cookie_secure: bool = False
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    revoke_on_password_change: bool = True


class LockoutConfig(_Base):
    enabled: bool = True
    max_attempts: int = 5
    window: Seconds = 15 * 60
    duration: Seconds = 30 * 60
    track_by_ip: bool = True


class TotpConfig(_Base):
    enabled: bool = True
    required_for_roles: list[str] = ["admin"]
    issuer: str = "Drive"


class HeadersConfig(_Base):
    hsts: bool = False
    content_security_policy: str | None = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'"
    )
    frame_options: str | None = "DENY"
    referrer_policy: str | None = "same-origin"


class SecurityConfig(_Base):
    secret_key: str = "CAMBIAME-EN-PRODUCCION"
    password_hash: Literal["argon2", "bcrypt"] = "argon2"
    password_policy: PasswordPolicy = PasswordPolicy()
    session: SessionConfig = SessionConfig()
    lockout: LockoutConfig = LockoutConfig()
    totp: TotpConfig = TotpConfig()
    headers: HeadersConfig = HeadersConfig()


class RateLimitRule(_Base):
    requests: int
    window: Seconds = 60


class RateLimitConfig(_Base):
    enabled: bool = True
    login: RateLimitRule = RateLimitRule(requests=10, window=300)
    # Se llama "signup" y no "register" porque pydantic reserva ese nombre.
    signup: RateLimitRule = RateLimitRule(requests=5, window=3600)
    api: RateLimitRule = RateLimitRule(requests=600, window=60)
    download: RateLimitRule = RateLimitRule(requests=300, window=60)
    exempt_networks: list[str] = ["127.0.0.1/32"]


class NetworkConfig(_Base):
    trusted_networks: list[str] = []
    trusted_mode: Literal["none", "readonly", "full"] = "none"
    trusted_user: str | None = None
    trusted_scope: Literal["public", "shared", "trusted_user"] = "public"
    require_login_for: list[str] = ["upload", "delete", "share", "admin", "settings"]
    allow_session_ip_change: bool = True
    denied_networks: list[str] = []


class SharingConfig(_Base):
    internal_enabled: bool = True
    public_links_enabled: bool = True
    public_links_roles: list[str] = ["admin", "user"]
    require_password_on_public_links: bool = False
    default_link_expiry: Seconds = 30 * 86400
    max_link_expiry: Seconds = 365 * 86400
    allow_upload_links: bool = True
    allow_reshare: bool = False
    token_length: int = 22
    default_download_limit: int | None = None


class RoleConfig(_Base):
    quota: Size = None
    max_upload_size: Size = None
    can_share_public: bool = True
    can_share_internal: bool = True
    can_upload: bool = True
    can_delete: bool = True
    can_manage_users: bool = False
    can_edit_settings: bool = False

    @field_validator("quota", "max_upload_size")
    @classmethod
    def _no_limit_is_none(cls, v: int | None) -> int | None:
        return _normalise_limit(v)


class LoggingConfig(_Base):
    level: Literal["debug", "info", "warning", "error"] = "info"
    file: str | None = None
    access_log: bool = True


class AuditConfig(_Base):
    enabled: bool = True
    events: list[str] = [
        "login",
        "login_failed",
        "logout",
        "upload",
        "download",
        "delete",
        "share_create",
        "share_access",
        "user_create",
        "settings_change",
    ]
    retention: Seconds = 180 * 86400
    store_ip: bool = True


class UiConfig(_Base):
    # Ya no se usa (sólo hay tema oscuro); se acepta para no romper configs antiguas.
    theme: Literal["auto", "light", "dark"] = "auto"
    items_per_page: int = 100
    show_trusted_network_hint: bool = True
    footer_text: str | None = None


class MaintenanceConfig(_Base):
    enabled: bool = True
    interval: Seconds = 3600
    purge_trash: bool = True
    purge_versions: bool = True
    purge_sessions: bool = True
    purge_audit: bool = True
    purge_orphan_blobs: bool = True


# --------------------------------------------------------------------------- #
# Raíz
# --------------------------------------------------------------------------- #


class Config(_Base):
    app: AppConfig = AppConfig()
    server: ServerConfig = ServerConfig()
    database: DatabaseConfig = DatabaseConfig()
    storage: StorageConfig = StorageConfig()
    trash: TrashConfig = TrashConfig()
    versions: VersionsConfig = VersionsConfig()
    thumbnails: ThumbnailsConfig = ThumbnailsConfig()
    auth: AuthConfig = AuthConfig()
    security: SecurityConfig = SecurityConfig()
    rate_limit: RateLimitConfig = RateLimitConfig()
    network: NetworkConfig = NetworkConfig()
    sharing: SharingConfig = SharingConfig()
    roles: dict[str, RoleConfig] = Field(
        default_factory=lambda: {
            "admin": RoleConfig(can_manage_users=True, can_edit_settings=True),
            "user": RoleConfig(quota=10 * 1024**3),
            "guest": RoleConfig(
                quota=512 * 1024**2,
                can_share_public=False,
                can_share_internal=False,
                can_upload=False,
                can_delete=False,
            ),
        }
    )
    logging: LoggingConfig = LoggingConfig()
    audit: AuditConfig = AuditConfig()
    ui: UiConfig = UiConfig()
    maintenance: MaintenanceConfig = MaintenanceConfig()

    # Redes ya parseadas, cacheadas para no re-parsear en cada petición.
    _networks: dict[str, list[Any]] = PrivateAttr(default_factory=dict)

    def role(self, name: str) -> RoleConfig:
        """Perfil de un rol, con fallback al perfil más restrictivo."""
        return self.roles.get(name) or self.roles.get("guest") or RoleConfig()

    def networks(self, key: str) -> list[Any]:
        """Lista de redes de una clave de configuración, ya parseada."""
        if key not in self._networks:
            raw: list[str] = {
                "trusted": self.network.trusted_networks,
                "denied": self.network.denied_networks,
                "proxies": self.server.trusted_proxies,
                "rate_limit_exempt": self.rate_limit.exempt_networks,
            }[key]
            self._networks[key] = [
                ipaddress.ip_network(n, strict=False) for n in raw
            ]
        return self._networks[key]

    def validate_runtime(self) -> list[str]:
        """Avisos de configuración peligrosa, emitidos al arrancar."""
        warnings: list[str] = []
        if self.security.secret_key == "CAMBIAME-EN-PRODUCCION":
            warnings.append(
                "security.secret_key sigue siendo el valor por defecto: "
                "cualquiera puede falsificar sesiones. Cámbialo ya."
            )
        if self.app.base_url.startswith("https://") and not self.security.session.cookie_secure:
            warnings.append(
                "base_url es https pero security.session.cookie_secure es false: "
                "la cookie de sesión viajará también por http."
            )
        if self.network.trusted_mode == "full" and not self.network.trusted_user:
            warnings.append(
                "network.trusted_mode es 'full' pero network.trusted_user está "
                "vacío: el acceso por red de confianza quedará desactivado."
            )
        if self.network.trusted_mode != "none" and not self.network.trusted_networks:
            warnings.append(
                "network.trusted_mode está activo pero no hay trusted_networks "
                "definidas: ningún cliente será considerado de confianza."
            )
        if self.server.behind_proxy and not self.server.trusted_proxies:
            warnings.append(
                "server.behind_proxy es true sin trusted_proxies: se aceptarán "
                "cabeceras X-Forwarded-For de cualquier origen, lo que permite "
                "falsificar la IP del cliente y saltarse las redes de confianza."
            )
        return warnings


# --------------------------------------------------------------------------- #
# Carga
# --------------------------------------------------------------------------- #


def _coerce_env(raw: str) -> Any:
    """Interpreta un valor de entorno como JSON si puede; si no, como texto."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


#: Variables con el prefijo que NO son ajustes, sino control del propio arranque.
RESERVED_ENV = {"DRIVE_CONFIG"}


def _apply_env_overrides(data: dict[str, Any], prefix: str = "DRIVE_") -> dict[str, Any]:
    for key, raw in os.environ.items():
        if not key.startswith(prefix) or key in RESERVED_ENV:
            continue
        path = key[len(prefix) :].lower().split("__")
        cursor = data
        for part in path[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        cursor[path[-1]] = _coerce_env(raw)
    return data


def load_config(
    path: str | Path | None = None, overrides: dict[str, Any] | None = None
) -> Config:
    """Construye la configuración: YAML < overrides de BD < entorno.

    El entorno queda por encima de todo a propósito: es la vía para recuperar
    una instancia cuyo panel de ajustes haya quedado en un estado que impida
    entrar (por ejemplo tras desactivar el login).
    """
    data = raw_config(path)
    if overrides:
        data = deep_merge(data, overrides)
    data = _apply_env_overrides(data)
    return Config.model_validate(data)


_active: Config | None = None


def get_config() -> Config:
    """Configuración activa del proceso."""
    global _active
    if _active is None:
        _active = load_config()
    return _active


def set_active_config(config: Config) -> None:
    """Sustituye la configuración activa (usado al aplicar overrides de BD)."""
    global _active
    _active = config


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Mezcla ``overlay`` sobre ``base`` sin mutar ninguno de los dos."""
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def raw_config(path: str | Path | None = None) -> dict[str, Any]:
    """Diccionario del YAML tal cual, sin validar ni aplicar entorno."""
    path = Path(path or os.environ.get("DRIVE_CONFIG", "config/config.yaml"))
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
