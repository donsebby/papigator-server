"""Hintergrund-Worker, zweistufig:

  pending  --[OCR-Worker: Text/OCR, Thumbnail, Regeln]-->  tagging  --[LLM-Worker]-->  done

So wartet die OCR nicht auf das LLM und umgekehrt. Schlägt das LLM fehl (Mac schläft),
wird das Dokument trotzdem 'done' mit llm_status='failed' und später automatisch nachgeholt.
"""
import logging
import threading
import time

from . import config, llm, processing
from .db import connect, get_setting

log = logging.getLogger("papigator.worker")
_wake_ocr = threading.Event()
_wake_llm = threading.Event()
_llm_idle = threading.Event()   # gesetzt = kein interaktiver LLM-Nutzer (Fragen) aktiv
_llm_idle.set()
_interactive = 0
_interactive_lock = threading.Lock()


class interactive_llm:
    """Kontext: Tagging-Worker macht Platz, solange ein Nutzer das LLM interaktiv braucht."""
    def __enter__(self):
        global _interactive
        with _interactive_lock:
            _interactive += 1
            _llm_idle.clear()
    def __exit__(self, *a):
        global _interactive
        with _interactive_lock:
            _interactive -= 1
            if _interactive <= 0:
                _interactive = 0
                _llm_idle.set()

LLM_RETRY_INTERVAL = 600  # Sekunden zwischen Nachhol-Versuchen


def wake() -> None:
    _wake_ocr.set()
    _wake_llm.set()


# ---------- Stufe 1: OCR ----------

def ocr_one(doc_id: int) -> None:
    pdf = processing.doc_path(doc_id)
    with connect() as con:
        con.execute("UPDATE documents SET status='processing', error=NULL WHERE id=?", (doc_id,))
        row = con.execute("SELECT text, text_source FROM documents WHERE id=?", (doc_id,)).fetchone()
    try:
        pages = processing.page_count(pdf)
        text, source = (row["text"] or ""), (row["text_source"] or "")
        if not text:
            text = processing.extract_text(pdf)
            source = "pdf"
            if not processing.has_enough_text(text, pages):
                log.info("OCR für Dokument %s", doc_id)
                ocr_text = processing.run_ocr(pdf)
                if len(ocr_text) > len(text):
                    text, source = ocr_text, "ocr"
        processing.make_thumbnail(pdf, processing.thumb_path(doc_id))
        with connect() as con:
            con.execute(
                "UPDATE documents SET text=?, text_source=?, page_count=?, status='tagging', llm_status='pending' WHERE id=?",
                (text, source, pages, doc_id),
            )
            processing.apply_rules(con, doc_id, text)
        log.info("Dokument %s: Text fertig (%s, %d Seiten)", doc_id, source, pages)
        _wake_llm.set()
    except Exception as e:
        log.exception("Dokument %s: OCR fehlgeschlagen", doc_id)
        with connect() as con:
            con.execute("UPDATE documents SET status='error', error=? WHERE id=?", (str(e)[:500], doc_id))


def ocr_loop() -> None:
    with connect() as con:
        con.execute("UPDATE documents SET status='pending' WHERE status='processing'")
    while True:
        with connect() as con:
            # Von außen Eingeworfenes zuletzt – es soll Scanner und Mail nicht aufhalten
            row = con.execute("SELECT id FROM documents WHERE status='pending' AND deleted_at IS NULL "
                              "ORDER BY ext_pending, llm_status IN ('refill', 'refill_w'), id LIMIT 1").fetchone()
        if row:
            try:
                ocr_one(row["id"])
            except Exception:
                log.exception("Dokument %s: OCR abgebrochen", row["id"])
                with connect() as con:
                    con.execute("UPDATE documents SET status='error' WHERE id=? AND status='processing'", (row["id"],))
            continue
        _wake_ocr.wait(timeout=10)
        _wake_ocr.clear()


# ---------- Stufe 2: LLM ----------

def llm_one(doc_id: int) -> bool:
    """True = LLM erreichbar (Ergebnis oder leer), False = Verbindung fehlgeschlagen."""
    with connect() as con:
        row = con.execute("SELECT text, llm_status FROM documents WHERE id=?", (doc_id,)).fetchone()
    # Nachtragen der Geld-Felder (Altbestand): nur diese Felder schreiben, nie „fehlgeschlagen“ melden –
    # sonst würde das Nachholen später ein komplettes Neu-Erkennen anstoßen und Korrekturen überschreiben
    # refill: Geld-Felder; refill_w: Zusatzfelder (Gewährleistung, Ablaufdaten, Wartungen; Bestand, 17.11.2026)
    refill = row["llm_status"] if row and row["llm_status"] in ("refill", "refill_w") else None
    with connect() as con:
        existing = [r["name"] for r in con.execute("SELECT name FROM tags ORDER BY name")]
        restricted = get_setting(con, "llm_tag_mode", "free") == "restricted"
    # Einstellungen aus der DB (Web/App), nicht nur der Env-Startwert – sonst bleibt eine nur im
    # Web eingetragene KI wirkungslos. Ausgeschaltet heißt „übersprungen“, nicht „fehlgeschlagen“.
    # Auch „übersprungen“: nur ein Cloud-Server eingetragen, der fürs Erkennen nicht freigegeben ist
    cfg = config.llm_config()
    if not cfg["enabled"] or not cfg["url"] or not (row and row["text"]) or not llm.usable("tag"):
        with connect() as con:
            if refill:   # bleibt vorgemerkt, bis eine KI da ist
                con.execute("UPDATE documents SET status='done' WHERE id=?", (doc_id,))
            else:
                con.execute("UPDATE documents SET status='done', llm_status='skipped', updated_at=datetime('now') WHERE id=?", (doc_id,))
        return True
    data, reachable = processing.llm_extract(row["text"], existing, restricted)
    with connect() as con:
        # Während das LLM lief, kann das Dokument gelöscht worden sein: dann nichts mehr schreiben
        # (Tags auf ein endgültig gelöschtes Dokument scheitern am Fremdschlüssel)
        if not con.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone():
            log.info("Dokument %s: während des Taggings gelöscht – Ergebnis verworfen", doc_id)
            return reachable
        if refill:
            if data:
                (processing.apply_extras if refill == "refill_w" else processing.apply_money)(con, doc_id, data)
            # leer, aber erreichbar: aufgeben; nicht erreichbar: vorgemerkt lassen, das Nachholen versucht es wieder
            con.execute("UPDATE documents SET status='done', llm_status=? WHERE id=?",
                        ("done" if data or reachable else refill, doc_id))
            log.info("Dokument %s: %s %s", doc_id, "Zusatzfelder" if refill == "refill_w" else "Geld-Felder",
                     "nachgetragen" if data else "nicht nachgetragen")
            return reachable
        if data:
            processing.apply_llm(con, doc_id, data, set(existing) if restricted else None)
            con.execute("UPDATE documents SET status='done', llm_status='done', updated_at=datetime('now') WHERE id=?", (doc_id,))
            log.info("Dokument %s: Tagging fertig", doc_id)
        else:
            con.execute("UPDATE documents SET status='done', llm_status='failed', updated_at=datetime('now') WHERE id=?", (doc_id,))
            log.warning("Dokument %s: Tagging fehlgeschlagen (%s)", doc_id, "nicht erreichbar" if not reachable else "keine Antwort")
    return reachable


# ---------- Pause ----------
# „KI-Erkennung pausieren“: nichts wird übersprungen, die Dokumente bleiben in der Warteschlange.
# Einstellung llm_paused_until: leer = läuft, "forever" = bis „Fortsetzen“, sonst Zeitpunkt (UTC, ISO).

def paused_until() -> str | None:
    """Ende der Pause (ISO, UTC) oder "forever"; None = läuft. Eine abgelaufene Pause wird dabei gelöscht."""
    from datetime import datetime, timezone
    with connect() as con:
        v = (get_setting(con, "llm_paused_until", "") or "").strip()
        if not v:
            return None
        if v == "forever":
            return v
        try:
            until = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            until = None
        if until is None or until <= datetime.now(timezone.utc):
            con.execute("DELETE FROM settings WHERE key='llm_paused_until'")
            return None
        return until.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def llm_loop() -> None:
    last_retry = 0.0
    backoff_until = 0.0
    while True:
        now = time.time()
        # Nachholen: fehlgeschlagene Taggings alle LLM_RETRY_INTERVAL erneut einreihen
        if now - last_retry > LLM_RETRY_INTERVAL:
            last_retry = now
            with connect() as con:
                n = con.execute(
                    "UPDATE documents SET status='tagging' WHERE status='done' AND llm_status IN ('failed', 'refill', 'refill_w') AND deleted_at IS NULL"
                ).rowcount
            if n:
                log.info("Tagging-Nachholen: %d Dokument(e) eingereiht", n)
        if now < backoff_until:
            _wake_llm.wait(timeout=backoff_until - now)
            _wake_llm.clear()
            continue
        if not _llm_idle.is_set():
            _llm_idle.wait(timeout=5)   # Nutzerfrage hat Vorrang
            continue
        if paused_until():
            _wake_llm.wait(timeout=30)   # pausiert: „Fortsetzen“ weckt sofort, sonst alle 30 s nachsehen
            _wake_llm.clear()
            continue
        with connect() as con:
            # Nachtragen im Bestand (refill) hinten anstellen – neue Scans sollen nicht dahinter warten
            row = con.execute("SELECT id FROM documents WHERE status='tagging' AND deleted_at IS NULL "
                              "ORDER BY ext_pending, id LIMIT 1").fetchone()
        if row:
            try:
                ok = llm_one(row["id"])
            except Exception:
                # Nie den ganzen Tagging-Thread sterben lassen – sonst bleibt alles bis zum Neustart liegen
                log.exception("Dokument %s: Tagging abgebrochen", row["id"])
                with connect() as con:
                    con.execute("UPDATE documents SET status='done', llm_status=CASE WHEN llm_status IN ('refill', 'refill_w') THEN 'done' "
                                "ELSE 'failed' END WHERE id=? AND status='tagging'", (row["id"],))
                continue
            if not ok:
                # LLM nicht erreichbar: alle wartenden auf failed setzen, 2 Minuten Pause
                with connect() as con:
                    con.execute("UPDATE documents SET status='done', llm_status='failed' WHERE status='tagging' AND llm_status NOT IN ('refill', 'refill_w')")
                    con.execute("UPDATE documents SET status='done' WHERE status='tagging' AND llm_status IN ('refill', 'refill_w')")
                backoff_until = time.time() + 120
                log.warning("LLM nicht erreichbar – Tagging pausiert, Nachholen in %ds", LLM_RETRY_INTERVAL)
            continue
        _wake_llm.wait(timeout=10)
        _wake_llm.clear()


def trash_loop() -> None:
    """Papierkorb: endgültig löschen nach trash_days."""
    while True:
        try:
            days = config.llm_config()["trash_days"]
            with connect() as con:
                rows = con.execute("SELECT id FROM documents WHERE deleted_at IS NOT NULL AND deleted_at < datetime('now', ?)",
                                   (f"-{days} days",)).fetchall()
                for r in rows:
                    con.execute("DELETE FROM documents WHERE id=?", (r["id"],))
            for r in rows:
                processing.doc_path(r["id"]).unlink(missing_ok=True)
                processing.thumb_path(r["id"]).unlink(missing_ok=True)
            if rows:
                log.info("Papierkorb: %d Dokument(e) endgültig gelöscht", len(rows))
        except Exception:
            log.exception("Papierkorb-Lauf fehlgeschlagen")
        time.sleep(3600)


def start() -> None:
    threading.Thread(target=ocr_loop, name="papigator-ocr", daemon=True).start()
    threading.Thread(target=llm_loop, name="papigator-llm", daemon=True).start()
    threading.Thread(target=trash_loop, name="papigator-trash", daemon=True).start()
    from . import backup
    threading.Thread(target=backup.loop, name="papigator-backup", daemon=True).start()
    from . import mail
    threading.Thread(target=mail.loop, name="papigator-mail", daemon=True).start()
