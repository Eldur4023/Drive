"""Inicio de sesión, registro y segundo factor."""

from __future__ import annotations

import base64
import io

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from .. import audit, ratelimit
from ..auth import (
    AuthError,
    authenticate,
    clear_session_cookie,
    create_session,
    get_principal,
    load_session,
    revoke_session,
    set_session_cookie,
)
from ..config import get_config
from ..database import get_db
from ..models import Role, User, utcnow
from ..netutils import client_ip, is_trusted_network
from ..permissions import Principal
from ..security import (
    check_password_policy,
    hash_password,
    new_totp_secret,
    totp_required,
    totp_uri,
    verify_totp,
)
from ..templating import render

router = APIRouter(tags=["auth"])


def _safe_next(target: str | None) -> str:
    """Evita redirecciones abiertas: sólo se aceptan rutas internas."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #


@router.get("/login")
def login_form(
    request: Request,
    next: str = "/",
    principal: Principal = Depends(get_principal),
    db: DbSession = Depends(get_db),
):
    if principal.is_authenticated and principal.kind == "user":
        return RedirectResponse(_safe_next(next), status.HTTP_303_SEE_OTHER)

    config = get_config()
    trusted = is_trusted_network(client_ip(request, config), config)
    return render(
        request,
        "login.html",
        principal,
        next=_safe_next(next),
        trusted_network=trusted,
        registration_open=config.auth.registration_enabled,
        # Si no hay ninguna cuenta todavía, se ofrece crear la primera.
        needs_bootstrap=db.scalar(select(func.count(User.id))) == 0,
    )


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    ip = client_ip(request, config)

    allowed, retry_after = ratelimit.check("login", str(ip), ip, config)
    if not allowed:
        return render(
            request,
            "login.html",
            None,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            error=f"Demasiados intentos. Espera {retry_after} segundos.",
            next=_safe_next(next),
            trusted_network=is_trusted_network(ip, config),
        )

    try:
        user = authenticate(db, username, password, str(ip) if ip else None, config)
    except AuthError as exc:
        audit.record(
            db, "login_failed", None, target_type="user", target_id=username,
            reason=exc.message, config=config,
        )
        db.commit()
        return render(
            request,
            "login.html",
            None,
            status_code=status.HTTP_401_UNAUTHORIZED,
            error=exc.message,
            next=_safe_next(next),
            username=username,
            trusted_network=is_trusted_network(ip, config),
        )

    ratelimit.reset("login", str(ip))

    # Si el usuario tiene 2FA activo, la sesión nace a medio hacer.
    pending = user.totp_enabled or totp_required(user.role.value, config)
    token = create_session(db, user, request, pending_totp=pending, config=config)

    if pending and not user.totp_enabled:
        # 2FA obligatorio por política pero aún no configurado: se le lleva a
        # configurarlo antes de poder usar nada.
        destination = "/2fa/setup"
    elif pending:
        destination = f"/2fa?next={_safe_next(next)}"
    else:
        destination = _safe_next(next)
        audit.record(db, "login", Principal(kind="user", user=user, ip=ip), config=config)
        db.commit()

    response = RedirectResponse(destination, status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, token, config)
    return response


@router.post("/logout")
def logout(
    request: Request,
    principal: Principal = Depends(get_principal),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    if principal.session_id:
        audit.record(db, "logout", principal, config=config)
        db.commit()
        revoke_session(db, principal.session_id)

    response = RedirectResponse("/login", status.HTTP_303_SEE_OTHER)
    clear_session_cookie(response, config)
    return response


# --------------------------------------------------------------------------- #
# Segundo factor
# --------------------------------------------------------------------------- #


def _pending_session(request: Request, db: DbSession):
    """Sesión a medio autenticar, esperando el código TOTP."""
    config = get_config()
    raw = request.cookies.get(config.security.session.cookie_name)
    if not raw:
        return None
    session = load_session(db, raw, config)
    return session if session is not None and session.pending_totp else None


@router.get("/2fa")
def totp_form(request: Request, next: str = "/", db: DbSession = Depends(get_db)):
    if _pending_session(request, db) is None:
        return RedirectResponse("/login", status.HTTP_303_SEE_OTHER)
    return render(request, "totp.html", None, next=_safe_next(next))


@router.post("/2fa")
def totp_verify(
    request: Request,
    code: str = Form(...),
    next: str = Form("/"),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    session = _pending_session(request, db)
    if session is None:
        return RedirectResponse("/login", status.HTTP_303_SEE_OTHER)

    user = session.user
    ip = client_ip(request, config)
    allowed, retry_after = ratelimit.check("login", f"totp:{user.id}", ip, config)
    if not allowed:
        return render(
            request, "totp.html", None,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            error=f"Demasiados intentos. Espera {retry_after} segundos.",
            next=_safe_next(next),
        )

    if not verify_totp(user.totp_secret or "", code):
        audit.record(db, "login_failed", None, target_id=user.id, reason="totp", config=config)
        db.commit()
        return render(
            request, "totp.html", None,
            status_code=status.HTTP_401_UNAUTHORIZED,
            error="Código incorrecto.",
            next=_safe_next(next),
        )

    session.pending_totp = False
    user.last_login_at = utcnow()
    audit.record(db, "login", Principal(kind="user", user=user, ip=ip), method="totp", config=config)
    db.commit()
    return RedirectResponse(_safe_next(next), status.HTTP_303_SEE_OTHER)


@router.get("/2fa/setup")
def totp_setup(
    request: Request,
    db: DbSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Alta del segundo factor. Accesible también con la sesión a medias."""
    config = get_config()
    user = principal.user
    if user is None:
        session = _pending_session(request, db)
        if session is None:
            return RedirectResponse("/login", status.HTTP_303_SEE_OTHER)
        user = session.user

    if not user.totp_secret or not user.totp_enabled:
        user.totp_secret = new_totp_secret()
        db.commit()

    uri = totp_uri(user.totp_secret, user.username, config)
    return render(
        request,
        "totp_setup.html",
        principal,
        secret=user.totp_secret,
        uri=uri,
        qr=_qr_data_uri(uri),
        already_enabled=user.totp_enabled,
    )


def _qr_data_uri(uri: str) -> str | None:
    """QR embebido en la página; sin él siempre queda el secreto en texto."""
    try:
        import qrcode

        img = qrcode.make(uri)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode()
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return None


@router.post("/2fa/setup")
def totp_enable(
    request: Request,
    code: str = Form(...),
    db: DbSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    user = principal.user
    session = None
    if user is None:
        session = _pending_session(request, db)
        if session is None:
            return RedirectResponse("/login", status.HTTP_303_SEE_OTHER)
        user = session.user

    if not verify_totp(user.totp_secret or "", code):
        uri = totp_uri(user.totp_secret or "", user.username)
        return render(
            request, "totp_setup.html", principal,
            status_code=status.HTTP_400_BAD_REQUEST,
            error="El código no coincide. Comprueba la hora del dispositivo.",
            secret=user.totp_secret, uri=uri, qr=_qr_data_uri(uri),
            already_enabled=False,
        )

    user.totp_enabled = True
    if session is not None:
        session.pending_totp = False
    db.commit()
    return RedirectResponse("/", status.HTTP_303_SEE_OTHER)


@router.post("/2fa/disable")
def totp_disable(
    password: str = Form(...),
    db: DbSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    from ..security import verify_password

    config = get_config()
    user = principal.user
    if user is None:
        return RedirectResponse("/login", status.HTTP_303_SEE_OTHER)
    if totp_required(user.role.value, config):
        return RedirectResponse(
            "/profile?error=La+política+exige+2FA+para+tu+rol", status.HTTP_303_SEE_OTHER
        )
    if not verify_password(password, user.password_hash):
        return RedirectResponse(
            "/profile?error=Contraseña+incorrecta", status.HTTP_303_SEE_OTHER
        )

    user.totp_enabled = False
    user.totp_secret = None
    db.commit()
    return RedirectResponse("/profile?ok=2FA+desactivado", status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- #
# Registro
# --------------------------------------------------------------------------- #


@router.get("/register")
def register_form(request: Request, db: DbSession = Depends(get_db)):
    config = get_config()
    first_user = db.scalar(select(func.count(User.id))) == 0
    if not config.auth.registration_enabled and not first_user:
        return render(
            request, "message.html", None,
            status_code=status.HTTP_403_FORBIDDEN,
            title="Registro cerrado",
            message="Esta instancia no admite registros. Pide una cuenta al administrador.",
        )
    return render(request, "register.html", None, first_user=first_user)


@router.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    email: str = Form(""),
    password: str = Form(...),
    password2: str = Form(...),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    ip = client_ip(request, config)
    first_user = db.scalar(select(func.count(User.id))) == 0

    if not config.auth.registration_enabled and not first_user:
        return render(
            request, "message.html", None,
            status_code=status.HTTP_403_FORBIDDEN,
            title="Registro cerrado",
            message="Esta instancia no admite registros.",
        )

    allowed, retry_after = ratelimit.check("signup", str(ip), ip, config)
    if not allowed:
        return render(
            request, "register.html", None,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            error=f"Demasiados intentos. Espera {retry_after} segundos.",
            first_user=first_user,
        )

    def fail(message: str):
        return render(
            request, "register.html", None,
            status_code=status.HTTP_400_BAD_REQUEST,
            error=message, username=username, email=email, first_user=first_user,
        )

    username = username.strip()
    email = email.strip().lower()

    if not username.isascii() or not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        return fail("El nombre de usuario sólo admite letras, números, punto, guion y guion bajo.")
    if len(username) < 3:
        return fail("El nombre de usuario debe tener al menos 3 caracteres.")
    if password != password2:
        return fail("Las contraseñas no coinciden.")

    errors = check_password_policy(password, username, email, config)
    if errors:
        return fail(" ".join(errors))

    if email and config.auth.allowed_email_domains:
        domain = email.rpartition("@")[2]
        if domain not in config.auth.allowed_email_domains:
            return fail("Ese dominio de correo no está admitido.")

    if db.scalar(select(User).where(User.username == username)):
        return fail("Ese nombre de usuario ya está en uso.")
    if email and db.scalar(select(User).where(User.email == email)):
        return fail("Ese correo ya está registrado.")

    user = User(
        username=username,
        email=email or None,
        password_hash=hash_password(password, config),
        # La primera cuenta de la instancia es siempre administradora: si no,
        # nadie podría aprobar al resto.
        role=Role.admin if first_user else Role(config.auth.default_role),
        is_approved=first_user or not config.auth.require_admin_approval,
    )
    db.add(user)
    audit.record(
        db, "user_create", None, target_type="user", target_id=user.username,
        self_registered=True, config=config,
    )
    db.commit()

    if not user.is_approved:
        return render(
            request, "message.html", None,
            title="Cuenta creada",
            message="Tu cuenta está pendiente de que un administrador la apruebe.",
        )

    token = create_session(db, user, request, config=config)
    response = RedirectResponse("/", status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, token, config)
    return response
