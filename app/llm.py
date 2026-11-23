"""KI-Server ansprechen: lokal (Ollama, LM Studio …) oder – nur mit ausdrücklicher Zustimmung – ein Cloud-Anbieter.

Sicherheitsregeln (Datenschutz zuerst):
- Ein Server gilt als **lokal**, wenn seine Adresse ins Heimnetz zeigt (private IP, Tailscale, localhost, Name ohne
  Punkt, .local/.lan/.internal oder ein Name, der intern auf eine private Adresse auflöst). Alles andere ist **Cloud**.
- An einen Cloud-Server geht kein Dokumenttext, solange ein Admin seinen Namen nicht ausdrücklich freigegeben hat
  (`llm_cloud_hosts`) – das prüft jede einzelne Anfrage, nicht nur das Speichern der Einstellungen.
- Pro Aufgabe abschaltbar: Erkennen (`llm_cloud_tag`) und Fragen (`llm_cloud_ask`).
- Jeder Server hat seinen eigenen Schlüssel (verschlüsselt gespeichert). Er geht nur an den Server, für den er
  eingegeben wurde; ändert sich der Servername, wird er gelöscht.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
import time
from urllib.parse import urlparse

import httpx

from . import config, security
from .db import connect, get_setting, set_setting

log = logging.getLogger("papigator.llm")

PURPOSES = ("tag", "ask")
_LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".localhost", ".home")
_dns_cache: dict[str, tuple[bool, float]] = {}
_no_temperature: set[tuple[str, str]] = set()   # (Host, Modell), die nur die Standard-Temperatur annehmen
_hung_until: dict[str, float] = {}               # Server-URL -> Zeitpunkt, bis zu dem er übersprungen wird
ALIVE_TIMEOUT = 6     # s: so lange darf ein lokaler Server für die Lebenszeichen-Abfrage brauchen
HUNG_PAUSE = 120      # s: so lange gilt ein hängender Server als weg


def host_of(url: str) -> str:
    try:
        return (urlparse((url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""


def _ip_is_local(value: str) -> bool:
    ip = ipaddress.ip_address(value.split("%")[0])
    # „nicht global“ deckt private Netze, Loopback, Link-Local und Tailscale (100.64.0.0/10) ab
    return not ip.is_global


def is_local(url: str) -> bool:
    """True = Heimnetz. Unklare Fälle (Name nicht auflösbar) gelten als Cloud – lieber einmal zu oft fragen."""
    host = host_of(url)
    if not host:
        return True
    if host == "localhost" or "." not in host or host.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        return _ip_is_local(host)
    except ValueError:
        pass
    hit = _dns_cache.get(host)
    if hit and time.time() - hit[1] < 300:
        return hit[0]
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(host, None)}
        local = bool(addrs) and all(_ip_is_local(a) for a in addrs)
    except (OSError, ValueError):
        local = False
    _dns_cache[host] = (local, time.time())
    return local


# ---------- Einstellungen ----------

def cloud_hosts(con) -> set[str]:
    try:
        return set(json.loads(get_setting(con, "llm_cloud_hosts", "[]") or "[]"))
    except ValueError:
        return set()


def _key_name(slot: int) -> str:
    return "llm_key" if slot == 1 else "llm_key2"


def stored_key(con, slot: int) -> str:
    return security.decrypt(get_setting(con, _key_name(slot), "") or "") or ""


def set_key(con, slot: int, key: str | None) -> None:
    key = (key or "").strip()
    set_setting(con, _key_name(slot), security.encrypt(key) if key else "")


def cloud_purposes(con) -> dict:
    return {"tag": get_setting(con, "llm_cloud_tag", "1") == "1",
            "ask": get_setting(con, "llm_cloud_ask", "1") == "1"}


def servers() -> list[dict]:
    """Eingetragene Server in Reihenfolge (Haupt-, dann Ersatz-Server) mit Schlüssel und Cloud-Status."""
    cfg = config.llm_config()
    out = []
    with connect() as con:
        hosts = cloud_hosts(con)
        for slot, url, model, ask_model in ((1, cfg["url"], cfg["model"], cfg.get("ask_model")),
                                            (2, cfg["url2"], cfg.get("model2") or cfg["model"], cfg.get("ask_model2"))):
            if not url:
                continue
            cloud = not is_local(url)
            out.append({"slot": slot, "url": url, "model": model, "ask_model": ask_model or model,
                        "host": host_of(url), "cloud": cloud,
                        "approved": (not cloud) or host_of(url) in hosts,
                        "key": stored_key(con, slot) or config.LLM_API_KEY})
    return out


def usable(purpose: str) -> list[dict]:
    """Server, die für diese Aufgabe Dokumenttext bekommen dürfen."""
    cfg = config.llm_config()
    if not cfg["enabled"]:
        return []
    with connect() as con:
        allowed = cloud_purposes(con)
    return [s for s in servers() if not s["cloud"] or (s["approved"] and allowed.get(purpose, False))]


def status() -> dict:
    """Für Einstellungen und Statusanzeigen (ohne Schlüssel)."""
    cfg = config.llm_config()
    with connect() as con:
        hosts = cloud_hosts(con)
        purposes = cloud_purposes(con)
        keys = {slot: bool(get_setting(con, _key_name(slot), "")) for slot in (1, 2)}
    info = {"llm_cloud_tag": purposes["tag"], "llm_cloud_ask": purposes["ask"]}
    for slot, url in ((1, cfg["url"]), (2, cfg["url2"])):
        sfx = "" if slot == 1 else "2"
        cloud = bool(url) and not is_local(url)
        info[f"llm_key{sfx}_set"] = keys[slot]
        info[f"llm_cloud{sfx}"] = cloud
        info[f"llm_cloud{sfx}_ok"] = cloud and host_of(url) in hosts
    return info


# ---------- Anfragen ----------

def chat(prompt: str, timeout: int = 180, purpose: str = "tag") -> str | None:
    """Eine Frage ans LLM, Antworttext zurück. None, wenn die KI aus ist oder für diese Aufgabe kein Server
    freigegeben ist. httpx-Fehler bei Verbindungsproblemen. Ist der erste Server nicht erreichbar, übernimmt
    der nächste."""
    candidates = usable(purpose)
    if not candidates:
        return None
    last: Exception | None = None
    for i, server in enumerate(candidates):
        if purpose == "ask":
            server = {**server, "model": server["ask_model"]}
        has_next = i + 1 < len(candidates)
        try:
            if has_next and not _alive(server):
                raise httpx.ConnectTimeout(f"{server['host']} antwortet nicht")
            return _post(server, prompt, timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            last = e
            if isinstance(e, httpx.ReadTimeout):
                _hung_until[server["url"].rstrip("/")] = time.monotonic() + HUNG_PAUSE
            if has_next:
                log.info("LLM %s nicht erreichbar (%s) – nehme Ersatz %s", server["host"], type(e).__name__,
                         candidates[i + 1]["host"])
    raise last  # type: ignore[misc]


def _alive(server: dict) -> bool:
    """Lebenszeichen eines lokalen Ollama-Servers, bevor er die eigentliche Anfrage bekommt: Ein halb
    schlafender Mac nimmt Verbindungen an, antwortet aber minutenlang nicht. Wer hängt, wird eine Weile
    übersprungen, damit nicht jede Anfrage erneut wartet. Cloud-Server und Nicht-Ollama-Server gelten als da."""
    url = server["url"].rstrip("/")
    if time.monotonic() < _hung_until.get(url, 0):
        return False
    if server["cloud"] or not url.endswith("/v1"):
        return True
    try:
        httpx.get(url[:-3] + "/api/version", timeout=ALIVE_TIMEOUT)
        return True   # auch 404 ist ein Lebenszeichen (kein Ollama, aber der Server antwortet)
    except httpx.HTTPError:
        _hung_until[url] = time.monotonic() + HUNG_PAUSE
        return False


def _headers(key: str | None) -> dict:
    return {"Authorization": f"Bearer {key}"} if key else {}


def _post(server: dict, prompt: str, timeout: int) -> str:
    """Bei Ollama (nur lokal) über dessen eigene API mit abgeschaltetem „Denken“: Modelle wie gemma4 grübeln über
    den OpenAI-kompatiblen Endpunkt sonst minutenlang. Kurzer Verbindungs-Timeout, damit ein abgeschalteter
    Server schnell zum Ersatz führt."""
    base, model = server["url"].rstrip("/"), server["model"]
    messages = [{"role": "user", "content": prompt}]
    tmo = httpx.Timeout(timeout, connect=5)
    if not server["cloud"] and base.endswith("/v1"):
        try:
            r = httpx.post(base[:-3] + "/api/chat",
                           json={"model": model, "messages": messages, "options": {"temperature": 0.1},
                                 "stream": False, "think": False}, timeout=tmo)
            if r.status_code == 200:
                return r.json()["message"]["content"]
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException):
            raise
        except Exception:
            pass  # kein Ollama (z. B. LM Studio, vLLM) – OpenAI-Weg nehmen
    return completion(base, model, messages, server.get("key"), tmo)


def completion(base: str, model: str, messages: list, key: str | None, timeout) -> str:
    """OpenAI-kompatibler Aufruf. Manche Cloud-Modelle (z. B. neuere Claude- und GPT-Modelle) lehnen eine eigene
    Temperatur ab – dann ohne sie wiederholen und das für dieses Modell merken."""
    body = {"model": model, "messages": messages, "stream": False}
    sig = (host_of(base), model)
    if sig not in _no_temperature:
        body["temperature"] = 0.1
    r = httpx.post(f"{base}/chat/completions", headers=_headers(key), json=body, timeout=timeout)
    if r.status_code == 400 and "temperature" in body and "temperature" in r.text.lower():
        _no_temperature.add(sig)
        body.pop("temperature")
        r = httpx.post(f"{base}/chat/completions", headers=_headers(key), json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def test(url: str, model: str, key: str | None) -> dict:
    """Verbindungstest: Modelle auflisten und eine Mini-Anfrage („Antworte nur mit: OK“) – ohne Dokumentinhalt,
    deshalb auch für noch nicht freigegebene Cloud-Server erlaubt."""
    base = url.rstrip("/")
    out = {"ok": False, "models": [], "reply": None, "error": None, "ms": None, "cloud": not is_local(url)}
    try:
        r = httpx.get(f"{base}/models", headers=_headers(key), timeout=8)
        if r.status_code in (401, 403):
            out["error"] = "Schlüssel fehlt oder ist ungültig"
            return out
        if r.status_code == 200:
            out["models"] = sorted(m.get("id") for m in r.json().get("data", []) if m.get("id"))
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        out["error"] = f"Server nicht erreichbar: {e.__class__.__name__}"
        return out
    except Exception:
        pass  # Modellliste ist nicht überall vorhanden – die Mini-Anfrage entscheidet
    if not model:
        out["ok"] = bool(out["models"])
        if not out["ok"]:
            out["error"] = "Keine Modellliste – bitte ein Modell eintragen und erneut testen"
        return out
    t0 = time.time()
    try:
        reply = completion(base, model, [{"role": "user", "content": "Antworte nur mit: OK"}], key,
                           httpx.Timeout(120, connect=8))
        out["reply"] = (reply or "").strip()[:60]
        out["ms"] = int((time.time() - t0) * 1000)
        out["ok"] = True
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        out["error"] = ("Schlüssel fehlt oder ist ungültig" if code in (401, 403)
                        else f"Modell antwortet nicht: HTTP {code} – {e.response.text[:120]}")
    except Exception as e:
        out["error"] = f"Anfrage fehlgeschlagen: {e.__class__.__name__}"
    return out
