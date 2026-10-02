"""Limitador de peticiones en memoria, por IP y familia de endpoint.

Ventana deslizante simple: basta para frenar fuerza bruta y raspado desde una
sola instancia. Si algún día se despliegan varios procesos detrás del mismo
balanceador, cada uno llevará su propia cuenta; para eso haría falta mover el
contador a Redis, pero no antes.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from .config import Config, get_config
from .netutils import IPAddress, in_networks

_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_lock = threading.Lock()
_last_cleanup = 0.0


def _cleanup(now: float) -> None:
    """Descarta cubos vacíos para que el diccionario no crezca sin fin."""
    global _last_cleanup
    if now - _last_cleanup < 300:
        return
    _last_cleanup = now
    for key in [k for k, v in _hits.items() if not v or now - v[-1] > 3600]:
        _hits.pop(key, None)


def check(
    bucket: str, identity: str, ip: IPAddress | None = None, config: Config | None = None
) -> tuple[bool, int]:
    """Registra un intento y dice si se permite.

    Devuelve ``(permitido, segundos_hasta_reintento)``.
    """
    config = config or get_config()
    if not config.rate_limit.enabled:
        return True, 0
    if ip is not None and in_networks(ip, config.networks("rate_limit_exempt")):
        return True, 0

    rule = getattr(config.rate_limit, bucket, None)
    if rule is None:
        return True, 0

    window = rule.window or 60
    now = time.monotonic()
    key = (bucket, identity)

    with _lock:
        _cleanup(now)
        hits = _hits[key]
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= rule.requests:
            retry_after = int(window - (now - hits[0])) + 1
            return False, retry_after
        hits.append(now)
        return True, 0


def reset(bucket: str, identity: str) -> None:
    """Limpia el contador, por ejemplo tras un login correcto."""
    with _lock:
        _hits.pop((bucket, identity), None)
