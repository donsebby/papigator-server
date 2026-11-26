"""Server automatisch finden (26.11.2026): Der Hauptdienst meldet sich per Bonjour/mDNS im Heimnetz als
`_papigator._tcp` – die iPhone/iPad-App zeigt ihn beim Anmelden unter „Im Netzwerk gefunden“.

- Nur der Hauptdienst (Port 8000, app/main.py), nie share_app. Standardmäßig an, aus mit PAPIGATOR_DISCOVERY=0;
  in der Demo immer aus. Die Tests setzen PAPIGATOR_DISCOVERY=0; uvicorn ohne --host (nur 127.0.0.1, so starten
  auch die Browser-Tests) meldet sich ebenfalls nicht an.
- TXT-Einträge: `version`, `path=/`, `name` (Rechnername) – nichts über Nutzer oder Dokumente.
- SRV-Ziel ist ein eigener Name `papigator-<rechner>.local.` mit genau einer IPv4 (die der Standardroute), nicht
  `<rechner>.local.`: Auf Unraid, HA OS oder einem Mac beantwortet den schon das System (Avahi, mDNSResponder) –
  abweichende Adressen (z. B. docker0) würden dort einen Namenskonflikt auslösen und den Rechner umbenennen.
- Docker im Bridge-Netz (Standard, auch Unraid): Multicast bleibt im Container-Netz, die App findet nichts. Dafür
  braucht es das Host-Netz – siehe README („Find the server automatically“).
- Robust: fehlt das Paket `zeroconf` (z. B. Deploy ohne pip install) oder klappt das Anmelden nicht, wird nur
  geloggt; der Server läuft weiter. Anmelden und Abmelden laufen in einem eigenen Thread – die synchrone
  zeroconf-API darf nicht im Event-Loop von uvicorn laufen (dort läuft das Startup-Ereignis).
"""
import logging
import os
import re
import socket
import sys
import threading
from typing import Optional

from . import config
from .version import VERSION

log = logging.getLogger(__name__)

SERVICE = "_papigator._tcp.local."
DEFAULT_PORT = 8000

_lock = threading.Lock()
_zc = None          # laufende Zeroconf-Instanz
_info = None        # angemeldeter Dienst (ServiceInfo)
_thread: Optional[threading.Thread] = None


def enabled() -> bool:
    if config.DEMO:
        return False
    return config.env("DISCOVERY", "1").strip().lower() not in ("0", "false", "no", "off")


def _arg(name: str) -> Optional[str]:
    """Wert einer uvicorn-Option aus der Befehlszeile (`--port 8000` oder `--port=8000`)."""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def loopback_only() -> bool:
    """uvicorn hört nur auf 127.0.0.1 (sein Standard ohne --host, z. B. beim lokalen Testen und in den
    Browser-Tests): dann ist der Server von anderen Geräten ohnehin nicht erreichbar – nicht anmelden."""
    host = _arg("--host") or os.environ.get("UVICORN_HOST")
    if host is None and not any("uvicorn" in a for a in sys.argv[:2]):
        return False                         # anders gestartet (gunicorn …): nicht raten
    return (host or "127.0.0.1").strip("[]") in ("127.0.0.1", "localhost", "::1")


def port() -> int:
    """Port, den uvicorn bedient: PAPIGATOR_DISCOVERY_PORT, sonst `--port` aus der Befehlszeile, sonst 8000.
    (Hinter einem Port-Mapping oder Reverse-Proxy den von außen erreichbaren Port per Env setzen.)"""
    raw = config.env("DISCOVERY_PORT", "") or _arg("--port") or os.environ.get("UVICORN_PORT")
    try:
        p = int(raw)
        return p if 0 < p < 65536 else DEFAULT_PORT
    except (TypeError, ValueError):
        return DEFAULT_PORT


def hostname() -> str:
    """Kurzer Rechnername, als DNS-Label tauglich („Tower“, „papigator“); leer → „papigator“."""
    h = (socket.gethostname() or "").split(".")[0]
    h = re.sub(r"[^A-Za-z0-9-]+", "-", h).strip("-")[:40]
    return h or "papigator"


def local_ip() -> Optional[str]:
    """IPv4 der Standardroute (UDP-„connect“ schickt kein Paket). None ohne Netz oder nur Loopback."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))          # TEST-NET-1, wird nie erreicht
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith(("127.", "0.", "169.254.")) else ip


def _server_label(host: str) -> str:
    """Eigener Name fürs SRV-Ziel, nie der Rechnername selbst (siehe oben)."""
    return host if host.lower().startswith("papigator") else f"papigator-{host}"


def _register() -> None:
    global _zc, _info
    try:
        from zeroconf import IPVersion, ServiceInfo, Zeroconf
    except ImportError:
        log.info("Bonjour: Paket zeroconf fehlt (pip install -r requirements.txt) – App findet den Server nicht automatisch")
        return
    ip = local_ip()
    if not ip:
        log.info("Bonjour: keine Netzwerkadresse gefunden – nicht angemeldet")
        return
    host, p = hostname(), port()
    zc = None
    try:
        zc = Zeroconf(interfaces=[ip], ip_version=IPVersion.V4Only)
        info = ServiceInfo(
            SERVICE, f"Papigator ({host}).{SERVICE}",
            addresses=[socket.inet_aton(ip)], port=p,
            properties={"version": VERSION, "path": "/", "name": host},
            server=f"{_server_label(host).lower()}.local.",
        )
        # Läuft schon ein zweiter Papigator mit gleichem Namen im Netz: „Papigator (x) (2)“
        zc.register_service(info, allow_name_change=True)
        with _lock:
            _zc, _info = zc, info
        log.info("Bonjour: %s auf %s:%s angemeldet", info.name, ip, p)
    except Exception as e:                   # Port 5353 belegt, kein Multicast, …: Server läuft trotzdem
        log.warning("Bonjour: Anmelden fehlgeschlagen: %s", e)
        if zc is not None:
            try:
                zc.close()
            except Exception:
                pass


def start() -> None:
    """Im Hintergrund anmelden; kehrt sofort zurück (das Anmelden prüft ein paar Sekunden auf Namenskonflikte)."""
    global _thread
    if not enabled():
        log.info("Bonjour: aus")
        return
    if loopback_only():
        log.info("Bonjour: Server hört nur auf 127.0.0.1 – nicht angemeldet")
        return
    with _lock:
        if _thread is not None or _zc is not None:
            return
        _thread = threading.Thread(target=_register, name="papigator-discovery", daemon=True)
        _thread.start()


def _unregister(zc, info) -> None:
    try:
        if info is not None:
            zc.unregister_service(info)
    except Exception as e:
        log.info("Bonjour: Abmelden: %s", e)
    finally:
        try:
            zc.close()
        except Exception:
            pass


def stop(timeout: float = 3.0) -> None:
    """Abmelden (Goodbye-Pakete, damit die App den Eintrag gleich entfernt), höchstens `timeout` Sekunden."""
    global _zc, _info, _thread
    t = _thread
    if t is not None:
        t.join(timeout)                      # Anmelden noch nicht fertig: kurz abwarten
    with _lock:
        zc, info = _zc, _info
        _zc = _info = _thread = None
    if zc is None:
        return
    worker = threading.Thread(target=_unregister, args=(zc, info), name="papigator-discovery-stop", daemon=True)
    worker.start()
    worker.join(timeout)
