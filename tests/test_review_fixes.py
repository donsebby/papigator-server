"""Funde aus der Prüfung vom 25.11.2026: Worker-Wettläufe und Dauerschleife, „Alle neu erkennen“ mit Nachtrag,
Art ohne Geld, Handkorrektur beendet Nachtragen, strengere Empfänger/Nummern, Wochenmail-Robustheit, Passwörter nur an
den gespeicherten Server, Ausgaben-Reihenfolge numerisch."""
import datetime as dt

from app import digest, processing, whatsnew, worker
from app.db import connect

TEXT = "Rechnung Nr. 2026-0042 an Mia Muster, Kundennummer KD 448812, Benachrichtigung vom 3.3.2026"


def _doc(admin, text=TEXT, title="Dok"):
    return admin.upload(text, title).json()["id"]


def _state(i):
    with connect() as con:
        r = con.execute("SELECT status, llm_status, title, doc_kind, direction, recipient FROM documents WHERE id=?", (i,)).fetchone()
    return dict(r)


def _llm_on(monkeypatch):
    from app import config, llm
    monkeypatch.setattr(config, "llm_config", lambda: {**config.llm_config.__wrapped__(), "enabled": True, "url": "http://127.0.0.1:1"}
                        if hasattr(config.llm_config, "__wrapped__") else {"enabled": True, "url": "http://127.0.0.1:1", "trash_days": 30})
    monkeypatch.setattr(llm, "usable", lambda purpose: [{"url": "x"}])


def test_result_discarded_when_reprocessed_meanwhile(admin, monkeypatch):
    i = _doc(admin)
    with connect() as con:
        con.execute("UPDATE documents SET status='tagging', llm_status='pending', title='Alter Titel' WHERE id=?", (i,))
    _llm_on(monkeypatch)

    def extract(text, existing, restricted):
        admin.post(f"/documents/{i}/reprocess")          # während die KI läuft: „Neu erkennen“
        return {"title": "Neuer Titel aus alter Runde"}, True
    monkeypatch.setattr(processing, "llm_extract", extract)
    worker.llm_one(i)
    s = _state(i)
    assert s["status"] == "pending" and s["llm_status"] is None and s["title"] == "Alter Titel"


def test_refill_without_text_does_not_loop(admin, monkeypatch):
    i = _doc(admin)
    with connect() as con:
        con.execute("UPDATE documents SET status='done', llm_status='refill_k', text=NULL WHERE id=?", (i,))
    monkeypatch.setattr(worker, "ai_usable", lambda: True)
    with connect() as con:
        assert worker.next_llm_doc(con) is None                 # ohne Text nie ausgewählt
    _llm_on(monkeypatch)
    worker.llm_one(i)                                           # falls doch: abschließen statt liegen lassen
    assert _state(i)["llm_status"] == "done"


def test_retag_all_turns_backfill_into_full_recognition(admin):
    i = _doc(admin)
    with connect() as con:
        con.execute("UPDATE documents SET status='done', llm_status='refill_k' WHERE id=?", (i,))
    assert admin.post("/settings/llm/retag?scope=all").status_code == 200
    s = _state(i)
    assert s["status"] == "tagging" and s["llm_status"] == "pending"


def test_kind_without_money_and_manual_edit_ends_backfill(admin):
    i = _doc(admin)
    with connect() as con:
        con.execute("UPDATE documents SET status='done', llm_status='refill_k', direction='out', amount=50 WHERE id=?", (i,))
    admin.patch(f"/documents/{i}", json={"doc_kind": "zeugnis"})
    s = _state(i)
    assert s["direction"] == "none" and s["llm_status"] == "done"
    # mit ausdrücklich geänderter Richtung bleibt sie
    admin.patch(f"/documents/{i}", json={"doc_kind": "angebot", "direction": "out"})
    assert _state(i)["direction"] == "out"


def test_web_style_full_save_keeps_backfill_and_detects_kind_change(admin):
    """Die Web-App schickt beim Speichern alle Felder mit (auch unveränderte Art/Empfänger/Nummern und die Richtung)."""
    i = _doc(admin)
    with connect() as con:
        con.execute("UPDATE documents SET status='done', llm_status='refill_k', direction='out', doc_kind='rechnung' WHERE id=?", (i,))
    full = {"title": "Neuer Titel", "doc_kind": "rechnung", "recipient": "", "refs": {}, "direction": "out", "amount": None}
    admin.patch(f"/documents/{i}", json=full)
    s = _state(i)
    assert s["llm_status"] == "refill_k" and s["title"] == "Neuer Titel"        # Nachtragen läuft weiter
    admin.patch(f"/documents/{i}", json={**full, "doc_kind": "zeugnis"})         # Art geändert, Richtung „out“ mitgeschickt
    s = _state(i)
    assert s["doc_kind"] == "zeugnis" and s["direction"] == "none" and s["llm_status"] == "done"


def test_backfill_to_non_money_kind_clears_direction(admin):
    i = _doc(admin)
    with connect() as con:
        con.execute("UPDATE documents SET doc_kind='info', direction='out' WHERE id=?", (i,))
        processing.apply_kind(con, i, {"kind": "Angebot"})
    s = _state(i)
    assert s["doc_kind"] == "angebot" and s["direction"] == "none"


def test_recipient_and_refs_need_whole_words():
    assert processing.recipient_from({"recipient": "Ben"}, TEXT) is None          # nur in „Benachrichtigung“
    assert processing.recipient_from({"recipient": "Mia Muster"}, TEXT) == "Mia Muster"
    refs = processing.refs_from({"references": {"invoice": "2026-0042", "customer": "KD448812", "other": "2026 3"}}, TEXT)
    assert refs == {"invoice": "2026-0042", "customer": "KD448812"}              # „2026 3“ gibt es so nicht


def test_whatsnew_ids_compare_as_numbers():
    assert whatsnew._key("2026-11-25.10") > whatsnew._key("2026-11-25.2") > whatsnew._key("2026-11-25") > whatsnew._key("0")


def test_digest_error_for_one_account_does_not_block_others(admin, sam, monkeypatch):
    import smtplib
    sent = []

    class Box:
        def __init__(self, *a, **k): pass
        def starttls(self, context=None): pass
        def login(self, u, p): pass
        def send_message(self, m): sent.append(m["To"])
        def noop(self): pass
        def quit(self): pass
    monkeypatch.setattr(digest.smtplib, "SMTP", Box)
    admin.patch("/settings/smtp", json={"smtp_host": "smtp.example.org", "smtp_port": 587, "smtp_security": "starttls",
                                        "smtp_user": "p@example.org", "smtp_password": "x", "smtp_from": "p@example.org"})
    for api, addr in ((admin, "a@example.org"), (sam, "s@example.org")):
        api.put("/digest", json={"enabled": True, "weekday": 0, "email": addr})
    real = digest.compose

    def compose(con, uid, now):
        if uid == admin.id:
            raise ValueError("kaputte Zeile")
        return real(con, uid, now)
    monkeypatch.setattr(digest, "compose", compose)
    monday = dt.datetime(2026, 11, 30, 8, 0)
    digest.run_due(monday)
    assert sent == ["s@example.org"]
    assert "kaputte Zeile" in (admin.get("/digest").json()["last_error"] or "")


def test_stored_passwords_only_go_to_stored_servers(admin, monkeypatch):
    logins = []

    class Box:
        def __init__(self, host, *a, **k): self.host = host
        def starttls(self, context=None): pass
        def login(self, u, p): logins.append((self.host, p))
        def noop(self): pass
        def quit(self): pass
    monkeypatch.setattr(digest.smtplib, "SMTP", Box)
    monkeypatch.setattr(digest.smtplib, "SMTP_SSL", Box)
    admin.patch("/settings/smtp", json={"smtp_host": "smtp.example.org", "smtp_port": 587, "smtp_security": "starttls",
                                        "smtp_user": "p@example.org", "smtp_password": "geheim", "smtp_from": "p@example.org"})
    admin.post("/settings/smtp/test", json={"smtp_host": "angreifer.example"})
    assert ("angreifer.example", "geheim") not in logins
    r = admin.post("/settings/smtp/test", json={"smtp_security": "none"}).json()
    assert r["ok"] is False and logins == []
    assert admin.post("/settings/smtp/test", json={}).json()["ok"] and logins == [("smtp.example.org", "geheim")]
