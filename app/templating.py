"""Entorno de plantillas y helpers de presentación."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from .config import get_config
from .permissions import Principal, action_allowed
from .storage import format_size

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _localtime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    try:
        tz = ZoneInfo(get_config().app.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    return value.replace(tzinfo=timezone.utc).astimezone(tz)


def fmt_datetime(value: datetime | None, fmt: str = "%d/%m/%Y %H:%M") -> str:
    local = _localtime(value)
    return local.strftime(fmt) if local else "—"


def fmt_relative(value: datetime | None) -> str:
    """Fechas cercanas en lenguaje natural; el resto, en formato corto."""
    if value is None:
        return "—"
    delta = datetime.now(timezone.utc).replace(tzinfo=None) - value
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "hace un momento"
    if seconds < 3600:
        return f"hace {seconds // 60} min"
    if seconds < 86400:
        return f"hace {seconds // 3600} h"
    if seconds < 7 * 86400:
        return f"hace {seconds // 86400} d"
    return fmt_datetime(value, "%d/%m/%Y")


def fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return "sin caducidad"
    for size, label in ((86400, "día"), (3600, "hora"), (60, "minuto")):
        if seconds >= size:
            value = seconds // size
            return f"{value} {label}{'s' if value != 1 else ''}"
    return f"{seconds} s"


def icon_for(node) -> Markup:  # noqa: ANN001 — recibe un Node
    """Icono SVG (del sprite de base.html) según el tipo de fichero."""
    kind = "file"
    mime = node.mime_type or ""
    if node.is_dir:
        kind = "folder"
    elif mime.startswith("image/"):
        kind = "image"
    elif mime.startswith("video/"):
        kind = "video"
    elif mime.startswith("audio/"):
        kind = "audio"
    elif "pdf" in mime:
        kind = "pdf"
    elif any(k in mime for k in ("zip", "compressed", "tar", "rar", "7z")):
        kind = "archive"
    elif any(k in mime for k in ("sheet", "excel", "csv")):
        kind = "sheet"
    elif mime.startswith("text/") or mime in ("application/json", "application/xml") or any(
        k in mime for k in ("word", "document")
    ):
        kind = "text"
    return Markup(f'<svg class="ico k-{kind}" aria-hidden="true"><use href="#i-{kind}"/></svg>')


def format_size_exact(num: int | None) -> str:
    """Para campos editables: sin redondeo, así guardar el formulario no lo altera."""
    if num is None:
        return ""
    for unit, factor in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if num and num % factor == 0:
            return f"{num // factor}{unit}"
    return f"{num}B"


templates.env.filters["size"] = format_size
templates.env.filters["size_exact"] = format_size_exact
templates.env.filters["datetime"] = fmt_datetime
templates.env.filters["relative"] = fmt_relative
templates.env.filters["duration"] = fmt_duration
templates.env.filters["icon"] = icon_for


def render(
    request: Request,
    template: str,
    principal: Principal | None = None,
    status_code: int = 200,
    **context,
) -> HTMLResponse:
    """Renderiza una plantilla con el contexto común ya resuelto."""
    config = get_config()
    can = {}
    if principal is not None:
        for action in ("upload", "delete", "share", "share_public", "admin", "settings"):
            can[action] = action_allowed(principal, action, config)[0]

    return templates.TemplateResponse(
        request,
        template,
        {
            "config": config,
            "principal": principal,
            "can": can,
            "app_name": config.app.name,
            "theme": (
                (principal.user.preferences or {}).get("theme")
                if principal and principal.user
                else None
            )
            or config.ui.theme,
            **context,
        },
        status_code=status_code,
    )
