"""Geheimnisse: Hauptschlüssel außerhalb der Datenbank, Verschlüsselung, Token-Prüfsummen.

Ziel: Eine Kopie der Datenbank (Sicherung, gestohlene Platte, Restore auf fremdem Rechner) enthält
nichts, womit man sich anmelden oder fremde Postfächer abrufen kann.

- Passwörter von Nutzern: bcrypt (auth.py).
- Anmelde-Tokens und Freigabe-Link-Tokens: nur als SHA-256 gespeichert; der Client hat das Original.
  Freigabe-Links zusätzlich verschlüsselt, damit man die Adresse später noch einmal kopieren kann.
- Mail-Passwort: verschlüsselt (Fernet = AES-128-CBC + HMAC-SHA256) mit dem Hauptschlüssel.
- Hauptschlüssel: PAPIGATOR_SECRET_KEY (z. B. in /etc/papigator.env, nur für root lesbar). Fehlt er,
  wird einmalig <DATA>/.papigator.key erzeugt (Rechte 600) – die eingebaute Sicherung nimmt ihn
  bewusst nicht mit. Ohne Schlüssel lässt sich das Mail-Passwort nicht entschlüsseln; dann einfach
  neu eingeben, alles andere funktioniert weiter.
"""
import base64
import hashlib
import hmac
import logging
import os
import re
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from . import config

log = logging.getLogger("papigator.security")
PREFIX = "enc:v1:"
KEY_FILE = config.DATA_DIR / ".papigator.key"
_HEX64 = re.compile(r"[0-9a-f]{64}")


@lru_cache(maxsize=1)
def _master() -> bytes:
    raw = config.env("SECRET_KEY", "")
    if raw:
        return hashlib.sha256(raw.encode()).digest()
    if KEY_FILE.exists():
        return base64.urlsafe_b64decode(KEY_FILE.read_text().strip())
    key = os.urandom(32)
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.urlsafe_b64encode(key).decode())
    log.warning("Kein PAPIGATOR_SECRET_KEY gesetzt – Schlüssel in %s erzeugt. Besser: als Umgebungsvariable "
                "außerhalb des Datenordners hinterlegen.", KEY_FILE)
    return key


def _derive(purpose: str) -> bytes:
    return hmac.new(_master(), purpose.encode(), hashlib.sha256).digest()


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(_derive("fernet")))


def encrypt(value: str | None) -> str | None:
    if not value:
        return value
    if value.startswith(PREFIX):
        return value
    return PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    """Klartext aus alten Versionen wird unverändert zurückgegeben (die Migration verschlüsselt ihn)."""
    if not value or not value.startswith(PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        log.error("Gespeichertes Geheimnis lässt sich nicht entschlüsseln – Schlüssel geändert? Bitte neu eingeben.")
        return None


def token_hash(token: str) -> str:
    """Prüfsumme für zufällige Tokens (≥ 190 Bit): SHA-256 genügt, ein Salz ist hier nicht nötig."""
    return hashlib.sha256(token.encode()).hexdigest()


def mac(purpose: str, value: str) -> str:
    return hmac.new(_derive(purpose), value.encode(), hashlib.sha256).hexdigest()


def migrate(con) -> None:
    """Ältere Datenbanken (oder ein zurückgespielter alter Snapshot) auf den sicheren Stand bringen.
    Mehrfach aufrufbar: erkennt schon umgestellte Einträge selbst."""
    n = 0
    for r in con.execute("SELECT rowid, token FROM sessions").fetchall():
        if not _HEX64.fullmatch(r["token"]):
            con.execute("UPDATE sessions SET token=? WHERE rowid=?", (token_hash(r["token"]), r["rowid"]))
            n += 1
    cols = {c[1] for c in con.execute("PRAGMA table_info(share_links)")}
    if "token_enc" not in cols:
        con.execute("ALTER TABLE share_links ADD COLUMN token_enc TEXT")
    for r in con.execute("SELECT id, token FROM share_links WHERE token_enc IS NULL").fetchall():
        con.execute("UPDATE share_links SET token=?, token_enc=? WHERE id=?",
                    (token_hash(r["token"]), encrypt(r["token"]), r["id"]))
        n += 1
    row = con.execute("SELECT value FROM settings WHERE key='mail_password'").fetchone()
    if row and row["value"] and not row["value"].startswith(PREFIX):
        con.execute("UPDATE settings SET value=? WHERE key='mail_password'", (encrypt(row["value"]),))
        n += 1
    # Früher lag hier ein eigener Schlüssel für Freigabe-Cookies; der kommt jetzt aus dem Hauptschlüssel
    con.execute("DELETE FROM settings WHERE key='share_secret'")
    if n:
        log.warning("Sicherheits-Umstellung: %d Geheimnis(se) gehasht bzw. verschlüsselt", n)


def harden_files() -> None:
    """Datenordner nur für den Dienst lesbar; der Eingangsordner bleibt für den Scanner-Nutzer offen."""
    try:
        config.DATA_DIR.chmod(0o711)                       # Durchgang für /inbox, aber kein Auflisten
        for d in (config.DOCS_DIR, config.THUMBS_DIR):
            d.chmod(0o700)
        for f in config.DATA_DIR.glob("*.db*"):
            f.chmod(0o600)
        if KEY_FILE.exists():
            KEY_FILE.chmod(0o600)
    except OSError as e:
        log.warning("Dateirechte konnten nicht gesetzt werden: %s", e)
