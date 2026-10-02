"""Sesiones, login y construcción del :class:`Principal` de cada petición."""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from .config import Config, get_config
from .database import get_db
from .models import ApiToken, LoginAttempt, Role, Session, User, utcnow
from .netutils import client_ip, is_denied_network, is_trusted_network
from .permissions import Principal, action_allowed, link_is_usable
from .security import (
    generate_token,
    hash_password,
    hash_token,
    needs_rehash,
    verify_password,
)

log = logging.getLogger(__name__)

SESSION_TOKEN_LENGTH = 48
LINK_SESSION_PREFIX = "link:"


class AuthError(Exception):
    """Fallo de autenticación con un mensaje mostrable al usuario."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


# --------------------------------------------------------------------------- #
# Bloqueo por intentos fallidos
# --------------------------------------------------------------------------- #


def _record_attempt(db: DbSession, key: str, success: bool) -> None:
    db.add(LoginAttempt(key=key, success=success))


def _recent_failures(db: DbSession, key: str, window: int) -> int:
    since = utcnow() - timedelta(seconds=window)
    rows = db.scalars(
        select(LoginAttempt).where(
            LoginAttempt.key == key,
            LoginAttempt.at >= since,
        )
    ).all()
    # Un acierto dentro de la ventana pone el contador a cero.
    failures = 0
    for row in sorted(rows, key=lambda r: r.at):
        failures = 0 if row.success else failures + 1
    return failures


def check_lockout(db: DbSession, key: str, config: Config) -> None:
    lockout = config.security.lockout
    if not lockout.enabled:
        return
    if _recent_failures(db, key, lockout.window or 900) >= lockout.max_attempts:
        raise AuthError(
            "Demasiados intentos fallidos. Prueba de nuevo más tarde.",
            retry_after=lockout.duration,
        )


def clear_attempts(db: DbSession, key: str) -> None:
    db.execute(delete(LoginAttempt).where(LoginAttempt.key == key))


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #


def find_user(db: DbSession, identifier: str, config: Config) -> User | None:
    identifier = identifier.strip()
    user = db.scalar(select(User).where(User.username == identifier))
    if user is None and config.auth.allow_email_login and "@" in identifier:
        user = db.scalar(select(User).where(User.email == identifier.lower()))
    return user


def authenticate(
    db: DbSession,
    identifier: str,
    password: str,
    ip: str | None = None,
    config: Config | None = None,
) -> User:
    """Valida credenciales. Lanza :class:`AuthError` con el motivo."""
    config = config or get_config()
    lockout = config.security.lockout

    user_key = f"user:{identifier.strip().lower()}"
    ip_key = f"ip:{ip}" if ip else None

    check_lockout(db, user_key, config)
    if ip_key and lockout.track_by_ip:
        check_lockout(db, ip_key, config)

    user = find_user(db, identifier, config)
    # Se verifica siempre un hash, exista el usuario o no, para que el tiempo
    # de respuesta no revele qué cuentas existen.
    stored = user.password_hash if user else hash_password("dummy", config)
    ok = verify_password(password, stored)

    if user is None or not ok:
        _record_attempt(db, user_key, False)
        if ip_key and lockout.track_by_ip:
            _record_attempt(db, ip_key, False)
        db.commit()
        raise AuthError("Usuario o contraseña incorrectos.")

    if user.locked_until is not None and user.locked_until > utcnow():
        raise AuthError("Esta cuenta está bloqueada temporalmente.")
    if not user.is_active:
        raise AuthError("Esta cuenta está desactivada.")
    if not user.is_approved:
        raise AuthError("Esta cuenta está pendiente de aprobación por un administrador.")

    _record_attempt(db, user_key, True)
    clear_attempts(db, user_key)
    if ip_key:
        clear_attempts(db, ip_key)

    if needs_rehash(user.password_hash, config):
        user.password_hash = hash_password(password, config)

    user.last_login_at = utcnow()
    db.commit()
    return user


# --------------------------------------------------------------------------- #
# Sesiones
# --------------------------------------------------------------------------- #


def create_session(
    db: DbSession,
    user: User,
    request: Request | None = None,
    *,
    from_trusted_network: bool = False,
    pending_totp: bool = False,
    config: Config | None = None,
) -> str:
    """Crea una sesión y devuelve el token en claro (sólo se ve aquí)."""
    config = config or get_config()
    settings = config.security.session
    raw = generate_token(SESSION_TOKEN_LENGTH)
    now = utcnow()

    session = Session(
        id=hash_token(raw),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(seconds=settings.lifetime or 604800),
        absolute_expires_at=(
            now + timedelta(seconds=settings.absolute_lifetime)
            if settings.absolute_lifetime
            else None
        ),
        ip=str(client_ip(request, config)) if request else None,
        user_agent=(request.headers.get("user-agent", "")[:255] if request else None),
        from_trusted_network=from_trusted_network,
        pending_totp=pending_totp,
    )
    db.add(session)
    db.commit()
    return raw


def load_session(db: DbSession, raw_token: str, config: Config) -> Session | None:
    session = db.get(Session, hash_token(raw_token))
    if session is None or session.revoked:
        return None

    now = utcnow()
    if session.expires_at <= now:
        return None
    if session.absolute_expires_at is not None and session.absolute_expires_at <= now:
        return None

    # Renovación deslizante: cada uso empuja la caducidad, sin pasar del tope.
    settings = config.security.session
    session.last_seen_at = now
    new_expiry = now + timedelta(seconds=settings.lifetime or 604800)
    if session.absolute_expires_at is not None:
        new_expiry = min(new_expiry, session.absolute_expires_at)
    session.expires_at = new_expiry
    return session


def revoke_session(db: DbSession, session_id: str) -> None:
    session = db.get(Session, session_id)
    if session is not None:
        session.revoked = True
        db.commit()


def revoke_all_sessions(db: DbSession, user_id: str, except_id: str | None = None) -> int:
    count = 0
    for session in db.scalars(
        select(Session).where(Session.user_id == user_id, Session.revoked.is_(False))
    ):
        if except_id and session.id == except_id:
            continue
        session.revoked = True
        count += 1
    db.commit()
    return count


def set_session_cookie(response: Response, token: str, config: Config) -> None:
    settings = config.security.session
    response.set_cookie(
        settings.cookie_name,
        token,
        max_age=settings.lifetime,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )


def clear_session_cookie(response: Response, config: Config) -> None:
    response.delete_cookie(config.security.session.cookie_name, path="/")


# --------------------------------------------------------------------------- #
# Tokens de API
# --------------------------------------------------------------------------- #


def load_api_token(db: DbSession, raw: str) -> ApiToken | None:
    token = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(raw)))
    if token is None or token.revoked:
        return None
    if token.expires_at is not None and token.expires_at <= utcnow():
        return None
    token.last_used_at = utcnow()
    return token


# --------------------------------------------------------------------------- #
# Dependencia principal
# --------------------------------------------------------------------------- #


def get_principal(
    request: Request,
    db: DbSession = Depends(get_db),
) -> Principal:
    """Identifica al autor de la petición combinando todas las vías de acceso."""
    config = get_config()
    ip = client_ip(request, config)

    if is_denied_network(ip, config):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Acceso no permitido desde tu red.")

    trusted = is_trusted_network(ip, config)
    principal = Principal(kind="anonymous", ip=ip, trusted_network=trusted, readonly=True)

    # 1. Token de API por cabecera.
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = load_api_token(db, header[7:].strip())
        if token is not None and token.user.is_active and token.user.is_approved:
            db.commit()
            return Principal(
                kind="token",
                user=token.user,
                ip=ip,
                trusted_network=trusted,
                scopes=list(token.scopes or []),
                readonly="write" not in (token.scopes or []),
            )

    # 2. Cookie de sesión.
    raw = request.cookies.get(config.security.session.cookie_name)
    if raw:
        session = load_session(db, raw, config)
        if session is not None and not session.pending_totp:
            user = session.user
            ip_changed = session.ip is not None and str(ip) != session.ip
            if ip_changed and not config.network.allow_session_ip_change:
                session.revoked = True
                db.commit()
            elif user.is_active and user.is_approved:
                db.commit()
                return Principal(
                    kind="network" if session.from_trusted_network else "user",
                    user=user,
                    session_id=session.id,
                    ip=ip,
                    trusted_network=trusted,
                    readonly=(
                        session.from_trusted_network
                        and config.network.trusted_mode == "readonly"
                    ),
                )
        db.commit()

    # 3. Red de confianza sin sesión: se suplanta al usuario configurado.
    if trusted and config.network.trusted_mode == "full" and config.network.trusted_user:
        user = db.scalar(select(User).where(User.username == config.network.trusted_user))
        if user is not None and user.is_active:
            return Principal(
                kind="network",
                user=user,
                ip=ip,
                trusted_network=trusted,
                readonly=False,
            )

    # 4. Anónimo. Sigue pudiendo ver lo público y, si la política lo permite,
    #    lo que la red de confianza abra.
    if trusted and config.network.trusted_mode != "none":
        principal.readonly = config.network.trusted_mode == "readonly"

    return principal


def require_user(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_authenticated:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Necesitas iniciar sesión.")
    return principal


def require_admin(principal: Principal = Depends(require_user)) -> Principal:
    if principal.user is None or principal.user.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Se requiere ser administrador.")
    return principal


def require_action(principal: Principal, action: str, config: Config | None = None) -> None:
    """Lanza 403 si la acción global no está permitida para este principal."""
    allowed, reason = action_allowed(principal, action, config)
    if not allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, reason or "Acción no permitida.")


__all__ = [
    "AuthError",
    "authenticate",
    "clear_session_cookie",
    "create_session",
    "get_principal",
    "link_is_usable",
    "load_session",
    "require_action",
    "require_admin",
    "require_user",
    "revoke_all_sessions",
    "revoke_session",
    "set_session_cookie",
]
