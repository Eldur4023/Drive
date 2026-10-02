"""Perfil del usuario: datos, preferencias, sesiones y tokens de API."""

from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from .. import audit, files_service
from ..auth import require_user, revoke_all_sessions
from ..config import get_config
from ..database import get_db
from ..models import ApiToken, Session, User, utcnow
from ..permissions import Principal
from ..security import (
    check_password_policy,
    generate_token,
    hash_password,
    hash_token,
    verify_password,
)
from ..templating import render

router = APIRouter(tags=["profile"])

VALID_SCOPES = ("read", "write", "share")


def _back(ok: str | None = None, error: str | None = None):
    params = {k: v for k, v in (("ok", ok), ("error", error)) if v}
    target = "/profile" + ("?" + urllib.parse.urlencode(params) if params else "")
    return RedirectResponse(target, status.HTTP_303_SEE_OTHER)


@router.get("/profile")
def profile(
    request: Request,
    ok: str | None = None,
    error: str | None = None,
    new_token: str | None = None,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    user = principal.user
    assert user is not None

    sessions = list(
        db.scalars(
            select(Session)
            .where(
                Session.user_id == user.id,
                Session.revoked.is_(False),
                Session.expires_at > utcnow(),
            )
            .order_by(Session.last_seen_at.desc())
        )
    )
    tokens = list(
        db.scalars(
            select(ApiToken)
            .where(ApiToken.user_id == user.id, ApiToken.revoked.is_(False))
            .order_by(ApiToken.created_at.desc())
        )
    )

    return render(
        request,
        "profile.html",
        principal,
        usage=files_service.usage_of(user, config),
        max_upload=files_service.max_upload_of(user, config),
        role_profile=config.role(user.role.value),
        sessions=sessions,
        current_session=principal.session_id,
        tokens=tokens,
        new_token=new_token,
        scopes=VALID_SCOPES,
        ok=ok,
        error=error,
    )


@router.post("/profile")
def update_profile(
    display_name: str = Form(""),
    email: str = Form(""),
    bio: str = Form(""),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    user = principal.user
    assert user is not None
    email = email.strip().lower()

    if email and email != (user.email or ""):
        if db.scalar(select(User).where(User.email == email, User.id != user.id)):
            return _back(error="Ese correo ya está en uso.")

    user.display_name = display_name.strip() or None
    user.email = email or None
    user.bio = bio.strip() or None
    db.commit()
    return _back(ok="Perfil actualizado.")


@router.post("/profile/preferences")
def update_preferences(
    order: str = Form("name"),
    view: str = Form("list"),
    items_per_page: int = Form(100),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    user = principal.user
    assert user is not None
    user.preferences = {
        **(user.preferences or {}),
        "order": order if order in ("name", "size", "modified", "created") else "name",
        "view": view if view in ("list", "grid") else "list",
        "items_per_page": max(10, min(500, items_per_page)),
    }
    db.commit()
    return _back(ok="Preferencias guardadas.")


@router.post("/profile/password")
def change_password(
    current: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    user = principal.user
    assert user is not None

    if not verify_password(current, user.password_hash):
        return _back(error="La contraseña actual no es correcta.")
    if password != password2:
        return _back(error="Las contraseñas nuevas no coinciden.")

    errors = check_password_policy(password, user.username, user.email, config)
    if errors:
        return _back(error=" ".join(errors))

    user.password_hash = hash_password(password, config)
    user.password_changed_at = utcnow()
    db.commit()

    if config.security.session.revoke_on_password_change:
        revoke_all_sessions(db, user.id, except_id=principal.session_id)
        return _back(ok="Contraseña cambiada. Se han cerrado las demás sesiones.")
    return _back(ok="Contraseña cambiada.")


# --------------------------------------------------------------------------- #
# Sesiones
# --------------------------------------------------------------------------- #


@router.post("/profile/sessions/{session_id}/revoke")
def revoke_one(
    session_id: str,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    session = db.get(Session, session_id)
    if session is None or session.user_id != principal.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Esa sesión no existe.")
    session.revoked = True
    db.commit()
    return _back(ok="Sesión cerrada.")


@router.post("/profile/sessions/revoke-all")
def revoke_others(
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    count = revoke_all_sessions(db, principal.user_id, except_id=principal.session_id)  # type: ignore[arg-type]
    return _back(ok=f"{count} sesión(es) cerradas.")


# --------------------------------------------------------------------------- #
# Tokens de API
# --------------------------------------------------------------------------- #


@router.post("/profile/tokens")
def create_token(
    name: str = Form(...),
    scopes: list[str] = Form([]),
    expires_days: int = Form(0),
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    from datetime import timedelta

    user = principal.user
    assert user is not None
    chosen = [s for s in scopes if s in VALID_SCOPES] or ["read"]

    raw = generate_token(40)
    token = ApiToken(
        user_id=user.id,
        name=name.strip()[:128] or "token",
        token_hash=hash_token(raw),
        scopes=chosen,
        expires_at=utcnow() + timedelta(days=expires_days) if expires_days else None,
    )
    db.add(token)
    audit.record(db, "settings_change", principal, target_type="api_token",
                 target_id=token.id, action="create")
    db.commit()

    # El token en claro se muestra una única vez.
    return RedirectResponse(
        f"/profile?new_token={urllib.parse.quote(raw)}", status.HTTP_303_SEE_OTHER
    )


@router.post("/profile/tokens/{token_id}/revoke")
def revoke_token(
    token_id: str,
    principal: Principal = Depends(require_user),
    db: DbSession = Depends(get_db),
):
    token = db.get(ApiToken, token_id)
    if token is None or token.user_id != principal.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ese token no existe.")
    token.revoked = True
    db.commit()
    return _back(ok="Token revocado.")
