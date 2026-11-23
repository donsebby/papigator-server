"""Echte Besucher-IP hinter einem Reverse-Proxy – für Sperren nach Fehlversuchen und Mengenbegrenzungen.

Weitergeleitete Angaben (X-Forwarded-For, X-Real-IP) setzt jeder Client beliebig. Sie zählen nur, wenn die Verbindung
selbst von einem vertrauten Proxy kommt (`PAPIGATOR_TRUSTED_PROXIES`). Bei einer Kette wird von rechts gelesen:
die erste Adresse, die nicht selbst ein vertrauter Proxy ist – die linken Einträge kann der Client fälschen."""
import ipaddress
from functools import lru_cache

from fastapi import Request

from . import config


@lru_cache(maxsize=1)
def _nets(spec: str):
    out = []
    for part in spec.split(","):
        part = part.strip()
        if part:
            try:
                out.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                pass
    return tuple(out)


def _trusted(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in _nets(config.TRUSTED_PROXIES))


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    if not peer or not _trusted(peer):
        return peer or ""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        chain = [p.strip() for p in fwd.split(",") if p.strip()]
        for ip in reversed(chain):
            if not _trusted(ip):
                return ip
        return chain[0] if chain else peer
    real = (request.headers.get("x-real-ip") or "").strip()
    return real or peer
