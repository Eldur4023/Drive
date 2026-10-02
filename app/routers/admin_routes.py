"""Panel de administración: usuarios, ajustes, auditoría y estado."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from .. import audit, files_service, settings_store, storage
from ..auth import require_admin, revoke_all_sessions
from ..config import get_config, parse_duration, parse_size
from ..database import get_db
from ..models import AuditLog, Blob, Node, Role, Session, SettingOverride, User, utcnow
from ..permissions import Principal
from ..security import check_password_policy, hash_password
from ..templating import render

router = APIRouter(prefix="/admin", tags=["admin"])


# --------------------------------------------------------------------------- #
# Catálogo de ajustes editables en caliente
# --------------------------------------------------------------------------- #


@dataclass
class Setting:
    key: str
    label: str
    kind: str  # bool | int | str | size | duration | choice | list
    help: str = ""
    options: tuple[str, ...] = ()


SETTINGS_GROUPS: dict[str, list[Setting]] = {
    "General": [
        Setting("app.name", "Nombre de la instancia", "str"),
        Setting("app.base_url", "URL pública", "str",
                "Se usa para construir los enlaces compartidos."),
        Setting("app.timezone", "Zona horaria", "str"),
        Setting("ui.theme", "Tema por defecto", "choice", options=("auto", "light", "dark")),
        Setting("ui.items_per_page", "Elementos por página", "int"),
        Setting("ui.footer_text", "Texto del pie", "str"),
    ],
    "Cuentas": [
        Setting("auth.registration_enabled", "Registro abierto", "bool"),
        Setting("auth.require_admin_approval", "Aprobar cuentas nuevas a mano", "bool"),
        Setting("auth.default_role", "Rol por defecto", "choice",
                options=("admin", "user", "guest")),
        Setting("auth.allow_email_login", "Permitir entrar con el correo", "bool"),
        Setting("auth.allowed_email_domains", "Dominios de correo admitidos", "list",
                "Separados por comas. Vacío = cualquiera."),
    ],
    "Seguridad": [
        Setting("security.password_policy.min_length", "Longitud mínima de contraseña", "int"),
        Setting("security.password_policy.require_uppercase", "Exigir mayúscula", "bool"),
        Setting("security.password_policy.require_digit", "Exigir dígito", "bool"),
        Setting("security.password_policy.require_symbol", "Exigir símbolo", "bool"),
        Setting("security.session.lifetime", "Duración de la sesión", "duration",
                "Ejemplos: 12h, 7d, 30d."),
        Setting("security.session.cookie_secure", "Cookie sólo por HTTPS", "bool",
                "Actívalo en cuanto sirvas por HTTPS."),
        Setting("security.lockout.enabled", "Bloqueo por intentos fallidos", "bool"),
        Setting("security.lockout.max_attempts", "Intentos antes de bloquear", "int"),
        Setting("security.lockout.duration", "Duración del bloqueo", "duration"),
        Setting("security.totp.enabled", "Segundo factor disponible", "bool"),
        Setting("security.totp.required_for_roles", "Roles obligados a usar 2FA", "list"),
    ],
    "Red local": [
        Setting("network.trusted_networks", "Redes de confianza", "list",
                "CIDR separados por comas: 192.168.1.0/24, 10.0.0.0/8."),
        Setting("network.trusted_mode", "Modo en red de confianza", "choice",
                "none: pedir login siempre. readonly: navegar y descargar sin login. "
                "full: sesión automática como el usuario indicado.",
                options=("none", "readonly", "full")),
        Setting("network.trusted_user", "Usuario suplantado en modo full", "str"),
        Setting("network.trusted_scope", "Qué se ve sin login desde la red", "choice",
                options=("public", "shared", "trusted_user")),
        Setting("network.require_login_for", "Acciones que siempre piden login", "list",
                "upload, delete, share, admin, settings, download."),
        Setting("network.allow_session_ip_change", "Permitir que la sesión cambie de IP", "bool"),
        Setting("network.denied_networks", "Redes bloqueadas", "list"),
    ],
    "Compartición": [
        Setting("sharing.internal_enabled", "Compartir entre usuarios", "bool"),
        Setting("sharing.public_links_enabled", "Enlaces públicos", "bool"),
        Setting("sharing.require_password_on_public_links", "Exigir contraseña en los enlaces", "bool"),
        Setting("sharing.default_link_expiry", "Caducidad por defecto", "duration"),
        Setting("sharing.max_link_expiry", "Caducidad máxima", "duration"),
        Setting("sharing.allow_upload_links", "Permitir enlaces de subida", "bool"),
        Setting("sharing.allow_reshare", "Permitir volver a compartir", "bool"),
    ],
    "Almacenamiento": [
        Setting("storage.max_upload_size", "Tamaño máximo por fichero", "size",
                "Ejemplos: 100MB, 2GB."),
        Setting("storage.default_quota", "Cuota por defecto", "size"),
        Setting("storage.blocked_extensions", "Extensiones bloqueadas", "list"),
        Setting("storage.allowed_extensions", "Extensiones permitidas", "list",
                "Si no está vacío actúa como lista blanca exclusiva."),
        Setting("trash.enabled", "Papelera", "bool"),
        Setting("trash.retention", "Retención en papelera", "duration"),
        Setting("versions.enabled", "Historial de versiones", "bool"),
        Setting("versions.max_per_file", "Versiones por fichero", "int"),
        Setting("thumbnails.enabled", "Miniaturas", "bool"),
    ],
    "Límites y registro": [
        Setting("rate_limit.enabled", "Límite de peticiones", "bool"),
        Setting("rate_limit.login.requests", "Intentos de login por ventana", "int"),
        Setting("rate_limit.download.requests", "Descargas por minuto", "int"),
        Setting("audit.enabled", "Auditoría", "bool"),
        Setting("audit.retention", "Retención de auditoría", "duration"),
        Setting("audit.store_ip", "Guardar la IP en la auditoría", "bool"),
        Setting("maintenance.enabled", "Tareas de mantenimiento", "bool"),
        Setting("maintenance.interval", "Intervalo de mantenimiento", "duration"),
    ],
}

ALL_SETTINGS = {s.key: s for group in SETTINGS_GROUPS.values() for s in group}


def _parse_setting(setting: Setting, raw: str) -> Any:
    raw = raw.strip()
    if setting.kind == "bool":
        return raw.lower() in ("1", "true", "on", "yes", "sí")
    if setting.kind == "int":
        return int(raw or 0)
    if setting.kind == "list":
        return [item.strip() for item in raw.split(",") if item.strip()]
    if setting.kind == "size":
        return None if not raw else parse_size(raw)
    if setting.kind == "duration":
        return None if not raw else parse_duration(raw)
    if setting.kind == "choice":
        if setting.options and raw not in setting.options:
            raise ValueError(f"valor no admitido: {raw}")
        return raw
    return raw or None


def _display(setting: Setting, value: Any) -> str:
    if value is None:
        return ""
    if setting.kind == "list":
        return ", ".join(str(v) for v in value)
    if setting.kind == "size":
        return storage.format_size(value) if value else ""
    if setting.kind == "duration":
        return _humanize_duration(value)
    return str(value)


def _humanize_duration(seconds: int) -> str:
    for size, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds % size == 0 and seconds >= size:
            return f"{seconds // size}{suffix}"
    return f"{seconds}s"


# --------------------------------------------------------------------------- #
# Panel
# --------------------------------------------------------------------------- #


def _redirect(path: str, ok: str | None = None, error: str | None = None):
    params = {k: v for k, v in (("ok", ok), ("error", error)) if v}
    if params:
        path += "?" + urllib.parse.urlencode(params)
    return RedirectResponse(path, status.HTTP_303_SEE_OTHER)


@router.get("")
def dashboard(
    request: Request,
    principal: Principal = Depends(require_admin),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    used, total = storage.disk_usage(config)
    stats = {
        "users": db.scalar(select(func.count(User.id))) or 0,
        "pending": db.scalar(select(func.count(User.id)).where(User.is_approved.is_(False))) or 0,
        "files": db.scalar(select(func.count(Node.id)).where(Node.is_dir.is_(False))) or 0,
        "public": db.scalar(select(func.count(Node.id)).where(Node.is_public.is_(True))) or 0,
        "blobs": db.scalar(select(func.count(Blob.hash))) or 0,
        "logical": db.scalar(select(func.coalesce(func.sum(Node.size), 0))
                             .where(Node.is_dir.is_(False))) or 0,
        "physical": db.scalar(select(func.coalesce(func.sum(Blob.size), 0))) or 0,
        "sessions": db.scalar(
            select(func.count(Session.id)).where(
                Session.revoked.is_(False), Session.expires_at > utcnow()
            )
        ) or 0,
        "disk_used": used,
        "disk_total": total,
    }
    return render(
        request, "admin_dashboard.html", principal,
        stats=stats, warnings=config.validate_runtime(),
        recent=list(db.scalars(select(AuditLog).order_by(AuditLog.at.desc()).limit(15))),
    )


# --------------------------------------------------------------------------- #
# Usuarios
# --------------------------------------------------------------------------- #


@router.get("/users")
def users(
    request: Request,
    ok: str | None = None,
    error: str | None = None,
    principal: Principal = Depends(require_admin),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    rows = list(db.scalars(select(User).order_by(User.username)))
    return render(
        request, "admin_users.html", principal,
        users=rows,
        usages={u.id: files_service.usage_of(u, config) for u in rows},
        roles=[r.value for r in Role],
        ok=ok, error=error,
    )


@router.post("/users")
def create_user(
    username: str = Form(...),
    email: str = Form(""),
    password: str = Form(...),
    role: str = Form("user"),
    quota: str = Form(""),
    principal: Principal = Depends(require_admin),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    username = username.strip()
    email = email.strip().lower()

    if db.scalar(select(User).where(User.username == username)):
        return _redirect("/admin/users", error="Ese nombre de usuario ya existe.")
    errors = check_password_policy(password, username, email, config)
    if errors:
        return _redirect("/admin/users", error=" ".join(errors))

    try:
        quota_bytes = parse_size(quota) if quota.strip() else None
    except ValueError as exc:
        return _redirect("/admin/users", error=str(exc))

    user = User(
        username=username,
        email=email or None,
        password_hash=hash_password(password, config),
        role=Role(role),
        is_approved=True,
        quota_override=quota_bytes,
    )
    db.add(user)
    audit.record(db, "user_create", principal, target_type="user", target_id=username,
                 role=role, config=config)
    db.commit()
    return _redirect("/admin/users", ok=f"Usuario {username} creado.")


@router.post("/users/{user_id}")
def update_user(
    user_id: str,
    role: str = Form(...),
    quota: str = Form(""),
    max_upload: str = Form(""),
    is_active: bool = Form(False),
    is_approved: bool = Form(False),
    can_upload: str = Form("inherit"),
    can_delete: str = Form("inherit"),
    can_share_public: str = Form("inherit"),
    can_share_internal: str = Form("inherit"),
    principal: Principal = Depends(require_admin),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ese usuario no existe.")

    # No se permite que el último administrador se degrade o se desactive:
    # dejaría la instancia sin nadie que pueda administrarla.
    admins = db.scalar(
        select(func.count(User.id)).where(User.role == Role.admin, User.is_active.is_(True))
    ) or 0
    losing_admin = user.role == Role.admin and (role != "admin" or not is_active)
    if losing_admin and admins <= 1:
        return _redirect("/admin/users", error="Debe quedar al menos un administrador activo.")

    try:
        user.quota_override = parse_size(quota) if quota.strip() else None
        user.max_upload_override = parse_size(max_upload) if max_upload.strip() else None
    except ValueError as exc:
        return _redirect("/admin/users", error=str(exc))

    user.role = Role(role)
    user.is_active = is_active
    user.is_approved = is_approved

    overrides = {}
    for name, value in (
        ("can_upload", can_upload),
        ("can_delete", can_delete),
        ("can_share_public", can_share_public),
        ("can_share_internal", can_share_internal),
    ):
        if value in ("yes", "no"):
            overrides[name] = value == "yes"
    user.permission_overrides = overrides

    if not is_active:
        revoke_all_sessions(db, user.id)

    audit.record(db, "settings_change", principal, target_type="user", target_id=user.username,
                 role=role, active=is_active, config=config)
    db.commit()
    return _redirect("/admin/users", ok=f"Usuario {user.username} actualizado.")


@router.post("/users/{user_id}/password")
def reset_password(
    user_id: str,
    password: str = Form(...),
    principal: Principal = Depends(require_admin),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ese usuario no existe.")

    errors = check_password_policy(password, user.username, user.email, config)
    if errors:
        return _redirect("/admin/users", error=" ".join(errors))

    user.password_hash = hash_password(password, config)
    user.password_changed_at = utcnow()
    revoke_all_sessions(db, user.id)
    audit.record(db, "settings_change", principal, target_type="user",
                 target_id=user.username, action="password_reset", config=config)
    db.commit()
    return _redirect("/admin/users", ok="Contraseña restablecida y sesiones cerradas.")


@router.post("/users/{user_id}/recompute")
def recompute(
    user_id: str,
    principal: Principal = Depends(require_admin),
    db: DbSession = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ese usuario no existe.")
    total = files_service.recompute_usage(db, user, get_config())
    db.commit()
    return _redirect("/admin/users", ok=f"Uso recalculado: {storage.format_size(total)}.")


@router.post("/users/{user_id}/delete")
def delete_user(
    user_id: str,
    confirm: str = Form(""),
    principal: Principal = Depends(require_admin),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ese usuario no existe.")
    if user.id == principal.user_id:
        return _redirect("/admin/users", error="No puedes borrar tu propia cuenta.")
    if confirm != user.username:
        return _redirect("/admin/users", error="Escribe el nombre de usuario para confirmar.")

    # Se borra el contenido nodo a nodo para liberar blobs y ajustar refcounts.
    for node in db.scalars(
        select(Node).where(Node.owner_id == user.id, Node.parent_id.is_(None))
    ).all():
        files_service.purge(db, node, config)

    audit.record(db, "settings_change", principal, target_type="user",
                 target_id=user.username, action="delete", config=config)
    db.delete(user)
    db.commit()
    return _redirect("/admin/users", ok="Usuario eliminado.")


# --------------------------------------------------------------------------- #
# Ajustes
# --------------------------------------------------------------------------- #


@router.get("/settings")
def settings_view(
    request: Request,
    ok: str | None = None,
    error: str | None = None,
    principal: Principal = Depends(require_admin),
    db: DbSession = Depends(get_db),
):
    config = get_config()
    overridden = {row.key for row in db.scalars(select(SettingOverride))}
    values = {
        key: _display(setting, settings_store.get_value(config, key))
        for key, setting in ALL_SETTINGS.items()
    }
    return render(
        request, "admin_settings.html", principal,
        groups=SETTINGS_GROUPS, values=values, overridden=overridden,
        locked=sorted(settings_store.LOCKED_KEYS), ok=ok, error=error,
    )


@router.post("/settings")
async def settings_save(
    request: Request,
    principal: Principal = Depends(require_admin),
    db: DbSession = Depends(get_db),
):
    form = await request.form()
    changed, errors = 0, []

    for key, setting in ALL_SETTINGS.items():
        field = key.replace(".", "__")
        if setting.kind == "bool":
            raw = "on" if form.get(field) else ""
        elif field not in form:
            continue
        else:
            raw = str(form.get(field, ""))

        try:
            value = _parse_setting(setting, raw)
        except (ValueError, TypeError) as exc:
            errors.append(f"{setting.label}: {exc}")
            continue

        current = settings_store.get_value(get_config(), key)
        if value == current:
            continue
        try:
            settings_store.set_override(db, key, value, principal.user_id)
            audit.record(db, "settings_change", principal, target_type="setting",
                         target_id=key, value=str(value))
            db.commit()
            changed += 1
        except ValueError as exc:
            errors.append(str(exc))

    message = f"{changed} ajuste(s) guardados." if changed else "Sin cambios."
    return _redirect("/admin/settings", ok=message, error=" ".join(errors) or None)


@router.post("/settings/reset")
def settings_reset(
    key: str = Form(...),
    principal: Principal = Depends(require_admin),
    db: DbSession = Depends(get_db),
):
    try:
        settings_store.clear_override(db, key)
    except Exception as exc:
        return _redirect("/admin/settings", error=str(exc))
    return _redirect("/admin/settings", ok=f"'{key}' vuelve al valor de config.yaml.")


# --------------------------------------------------------------------------- #
# Auditoría
# --------------------------------------------------------------------------- #


@router.get("/audit")
def audit_view(
    request: Request,
    event: str = "",
    user: str = "",
    page: int = 1,
    principal: Principal = Depends(require_admin),
    db: DbSession = Depends(get_db),
):
    per_page = 100
    query = select(AuditLog).order_by(AuditLog.at.desc())
    if event:
        query = query.where(AuditLog.event == event)
    if user:
        target = db.scalar(select(User).where(User.username == user.strip()))
        query = query.where(AuditLog.user_id == (target.id if target else "—"))

    page = max(1, page)
    rows = list(db.scalars(query.offset((page - 1) * per_page).limit(per_page)))
    names = {
        u.id: u.username
        for u in db.scalars(select(User).where(User.id.in_({r.user_id for r in rows if r.user_id})))
    }
    return render(
        request, "admin_audit.html", principal,
        rows=rows, names=names, event=event, user=user, page=page,
        events=get_config().audit.events,
    )
