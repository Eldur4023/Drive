"""Resolución de la IP real del cliente y evaluación de redes.

Todo el modo "no me pidas usuario si estoy en casa" descansa sobre saber de
verdad desde qué IP llega la petición. Si el servidor está detrás de un proxy
inverso hay que leerla de ``X-Forwarded-For``, pero esa cabecera la puede
escribir cualquiera: sólo se acepta cuando el salto inmediato es un proxy
declarado en ``server.trusted_proxies``.
"""

from __future__ import annotations

import ipaddress
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network

from fastapi import Request

from .config import Config, get_config

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network


def parse_ip(value: str | None) -> IPAddress | None:
    if not value:
        return None
    value = value.strip()
    # Los clientes IPv6 llegan como [::1]:1234 y los IPv4 como 1.2.3.4:1234.
    if value.startswith("[") and "]" in value:
        value = value[1 : value.index("]")]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def in_networks(ip: IPAddress | None, networks: list[IPNetwork]) -> bool:
    if ip is None or not networks:
        return False
    # Una dirección IPv4 mapeada en IPv6 (::ffff:192.168.1.5) debe compararse
    # como IPv4, o nunca casaría con las redes privadas habituales.
    candidates = [ip]
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped:
        candidates.append(ip.ipv4_mapped)
    return any(
        c in net for c in candidates for net in networks if c.version == net.version
    )


def client_ip(request: Request, config: Config | None = None) -> IPAddress | None:
    """IP real del cliente, teniendo en cuenta los proxies de confianza."""
    config = config or get_config()
    peer = parse_ip(request.client.host if request.client else None)

    if not config.server.behind_proxy:
        return peer

    if not in_networks(peer, config.networks("proxies")):
        # El salto inmediato no es un proxy nuestro: sus cabeceras no valen.
        return peer

    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Formato: "cliente, proxy1, proxy2". Recorremos de derecha a izquierda
        # descartando los proxies conocidos; el primero desconocido es el
        # cliente real (los anteriores pueden ser inventados por él).
        chain = [parse_ip(part) for part in forwarded.split(",")]
        for candidate in reversed(chain):
            if candidate is None:
                continue
            if in_networks(candidate, config.networks("proxies")):
                continue
            return candidate
        return peer

    real_ip = parse_ip(request.headers.get("x-real-ip"))
    return real_ip or peer


def client_scheme(request: Request, config: Config | None = None) -> str:
    config = config or get_config()
    if config.server.behind_proxy:
        peer = parse_ip(request.client.host if request.client else None)
        if in_networks(peer, config.networks("proxies")):
            proto = request.headers.get("x-forwarded-proto")
            if proto:
                return proto.split(",")[0].strip()
    return request.url.scheme


def is_trusted_network(ip: IPAddress | None, config: Config | None = None) -> bool:
    config = config or get_config()
    return in_networks(ip, config.networks("trusted"))


def is_denied_network(ip: IPAddress | None, config: Config | None = None) -> bool:
    config = config or get_config()
    return in_networks(ip, config.networks("denied"))


def matches_any(ip: IPAddress | None, cidrs: list[str]) -> bool:
    """Comprueba una IP contra una lista de CIDR en texto (enlaces, reglas)."""
    if not cidrs:
        return True  # lista vacía = sin restricción
    networks: list[IPNetwork] = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            continue
    return in_networks(ip, networks)
