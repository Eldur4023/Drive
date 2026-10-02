"""Generación de miniaturas para las vistas de galería."""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Config, get_config
from .storage import blob_path, thumb_path

log = logging.getLogger(__name__)

SUPPORTED_MIME_PREFIXES = ("image/",)
UNSUPPORTED_MIME = {"image/svg+xml", "image/heic", "image/heif"}


def can_thumbnail(mime_type: str | None, size: int, config: Config | None = None) -> bool:
    config = config or get_config()
    if not config.thumbnails.enabled or not mime_type:
        return False
    if mime_type in UNSUPPORTED_MIME:
        return False
    if config.thumbnails.max_source_size and size > config.thumbnails.max_source_size:
        return False
    return mime_type.startswith(SUPPORTED_MIME_PREFIXES)


def generate(digest: str, size: int, config: Config | None = None) -> Path | None:
    """Crea (o reutiliza) la miniatura de un blob. ``None`` si no se puede."""
    config = config or get_config()
    target = thumb_path(digest, size, config)
    if target.is_file():
        return target

    source = blob_path(digest, config)
    if not source.is_file():
        return None

    try:
        from PIL import Image, ImageOps

        with Image.open(source) as img:
            # exif_transpose respeta la orientación de las fotos de móvil.
            img = ImageOps.exif_transpose(img)
            img.thumbnail((size, size))
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            img.save(tmp, format="WEBP", quality=82, method=4)
            tmp.replace(target)
        return target
    except Exception:
        log.info("no se pudo generar miniatura de %s", digest, exc_info=True)
        return None
