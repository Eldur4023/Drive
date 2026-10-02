"""Registro de auditoría."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session as DbSession

from .config import Config, get_config
from .models import AuditLog, utcnow
from .permissions import Principal


def record(
    db: DbSession,
    event: str,
    principal: Principal | None = None,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    config: Config | None = None,
    **detail,
) -> None:
    """Anota un evento si está en la lista de eventos auditados.

    No hace commit: se une a la transacción de la operación que lo provoca, de
    forma que si la acción se deshace su rastro también.
    """
    config = config or get_config()
    if not config.audit.enabled or event not in config.audit.events:
        return

    db.add(
        AuditLog(
            event=event,
            user_id=principal.user_id if principal else None,
            actor=principal.label if principal else None,
            ip=(
                str(principal.ip)
                if principal and principal.ip and config.audit.store_ip
                else None
            ),
            target_type=target_type,
            target_id=target_id,
            detail=detail or {},
        )
    )


def purge(db: DbSession, config: Config | None = None) -> int:
    """Borra los registros más antiguos que la retención configurada."""
    config = config or get_config()
    if not config.audit.retention:
        return 0
    cutoff = utcnow() - timedelta(seconds=config.audit.retention)
    result = db.execute(delete(AuditLog).where(AuditLog.at < cutoff))
    return result.rowcount or 0
