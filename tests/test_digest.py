"""Wochenübersicht per Mail (app/digest.py): Einstellungen, Rechte, Inhalt ohne Fremdes und ohne Geld,
kein Doppelversand, verpasster Termin am selben Tag nachgeholt – alles mit einem falschen SMTP-Server."""
import datetime as dt
import email

import pytest

from app import config, digest
from app.db import connect

# Ein Montag in KW 48 (ISO), 08:00 Serverzeit
MONDAY = dt.datetime(2026, 11, 23, 8, 0)


class FakeSMTP:
    """Ersatz für smtplib.SMTP/SMTP_SSL: merkt sich Verbindungen und Nachrichten, verschickt nichts."""
    sent: list = []
    logins: list = []
    fail = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port
        if FakeSMTP.fail:
            raise OSError("Verbindung abgelehnt")

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        FakeSMTP.logins.append((user, password))

    def noop(self):
        return (250, b"OK")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.logins, FakeSMTP.fail = [], [], False
    monkeypatch.setattr(digest.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(digest.smtplib, "SMTP_SSL", FakeSMTP)
    digest._last_test.clear()
    return FakeSMTP


@pytest.fixture
def smtp(admin):
    r = admin.patch("/settings/smtp", json={"smtp_host": "smtp.example.org", "smtp_port": 587, "smtp_security": "starttls",
                                            "smtp_user": "papigator@example.org", "smtp_password": "smtp-geheim",
                                            "smtp_from": "papigator@example.org"})
    assert r.status_code == 200, r.text
    return r.json()


def _text(msg) -> str:
    return msg.get_body(preferencelist=("plain",)).get_content()


def _html(msg) -> str:
    return msg.get_body(preferencelist=("html",)).get_content()


def _enable(api, weekday=0, addr="alex@example.org"):
    r = api.put("/digest", json={"enabled": True, "weekday": weekday, "email": addr})
    assert r.status_code == 200, r.text
    return r.json()


# ---------- Einstellungen ----------

def test_default_off_and_explains_missing_smtp(admin):
    d = admin.get("/digest").json()
    assert d["enabled"] is False and d["email"] == "" and d["smtp_ready"] is False and d["hour"] == digest.SEND_HOUR
    assert len(d["weekdays"]) == 7
    r = admin.post("/digest/test")
    assert r.status_code == 400 and "nicht eingerichtet" in r.json()["detail"]
    assert digest.run_due(MONDAY) == 0


def test_validation(admin, smtp):
    assert admin.put("/digest", json={"enabled": True}).status_code == 400            # ohne Adresse
    assert admin.put("/digest", json={"email": "keine-adresse"}).status_code == 400
    assert admin.put("/digest", json={"email": "a@example.org\r\nBcc: x@example.org"}).status_code == 400
    assert admin.put("/digest", json={"weekday": 7, "email": "a@example.org"}).status_code == 400
    d = _enable(admin, weekday=4)
    assert d["enabled"] and d["weekday"] == 4 and d["email"] == "alex@example.org" and d["smtp_ready"]
    assert admin.put("/digest", json={"enabled": False}).json()["email"] == "alex@example.org"   # Adresse bleibt


def test_bootstrap_cannot_subscribe(client):
    r = client.put("/digest", json={"enabled": True, "email": "a@example.org"}, headers={"Authorization": "Bearer boot-token"})
    assert r.status_code == 400


def test_prefs_are_per_account(admin, sam, smtp):
    _enable(admin)
    assert sam.get("/digest").json()["enabled"] is False
    assert sam.get("/digest").json()["email"] == ""


def test_smtp_settings_admin_only_and_password_hidden(admin, sam, smtp):
    assert "smtp_password" not in smtp and smtp["smtp_password_set"] is True and smtp["configured"] is True
    with connect() as con:
        raw = con.execute("SELECT value FROM settings WHERE key='smtp_password'").fetchone()[0]
    assert raw.startswith("enc:") and "smtp-geheim" not in raw
    assert "smtp-geheim" not in admin.get("/settings/smtp").text
    assert "smtp-geheim" not in admin.get("/settings").text
    for method, url, body in [("get", "/settings/smtp", None), ("patch", "/settings/smtp", {"smtp_host": "x"}),
                              ("post", "/settings/smtp/test", {})]:
        kw = {"json": body} if body is not None else {}
        assert getattr(sam, method)(url, **kw).status_code == 403
    # leeres Passwort = unverändert
    admin.patch("/settings/smtp", json={"smtp_password": ""})
    assert admin.get("/settings/smtp").json()["smtp_password_set"] is True
    assert admin.patch("/settings/smtp", json={"smtp_security": "irgendwas"}).status_code == 400
    assert admin.patch("/settings/smtp", json={"smtp_from": "kaputt"}).status_code == 400


def test_smtp_test_connects_without_sending(admin, smtp, fake_smtp):
    r = admin.post("/settings/smtp/test", json={})
    assert r.json() == {"ok": True, "login": True}
    assert fake_smtp.logins == [("papigator@example.org", "smtp-geheim")] and fake_smtp.sent == []
    fake_smtp.fail = True
    r = admin.post("/settings/smtp/test", json={"smtp_host": "smtp.example.net"}).json()
    assert r["ok"] is False and "Verbindung abgelehnt" in r["error"]


def test_suggestion_from_imap_and_password_reuse(admin, fake_smtp):
    admin.patch("/settings/mail", json={"mail_host": "imap.example.org", "mail_user": "scan@example.org",
                                        "mail_password": "imap-geheim"})
    s = admin.get("/settings/smtp").json()
    assert s["configured"] is False
    assert s["suggestion"] == {"smtp_host": "smtp.example.org", "smtp_port": "587", "smtp_security": "starttls",
                               "smtp_user": "scan@example.org", "smtp_from": "scan@example.org"}
    # Testen mit den übernommenen, noch ungespeicherten Werten meldet sich schon mit dem IMAP-Passwort an
    assert admin.post("/settings/smtp/test", json=s["suggestion"]).json() == {"ok": True, "login": True}
    assert fake_smtp.logins[-1] == ("scan@example.org", "imap-geheim")
    s = admin.patch("/settings/smtp", json={k: v for k, v in s["suggestion"].items() if k != "smtp_port"}).json()
    assert s["configured"] and s["smtp_password_set"] and s["smtp_password_from_imap"]
    assert admin.post("/settings/smtp/test", json={}).json()["ok"]
    assert fake_smtp.logins[-1] == ("scan@example.org", "imap-geheim")


# ---------- Test-Mail ----------

def test_test_mail_goes_to_own_address_and_is_not_the_weekly(admin, smtp, fake_smtp):
    assert admin.post("/digest/test").status_code == 400                    # noch keine Adresse
    admin.put("/digest", json={"email": "alex@example.org"})                # Adresse ohne Einschalten reicht
    r = admin.post("/digest/test")
    assert r.status_code == 200, r.text
    assert r.json()["to"] == "alex@example.org"
    msg = fake_smtp.sent[-1]
    assert msg["To"] == "alex@example.org" and "papigator@example.org" in msg["From"]
    assert msg["Auto-Submitted"] == "auto-generated"
    assert msg.is_multipart() and _text(msg) and "<html" in _html(msg)
    assert "Test-Mail" in _text(msg)
    with connect() as con:
        assert con.execute("SELECT last_week FROM digest_prefs WHERE user_id=?", (admin.id,)).fetchone()[0] is None
    assert admin.post("/digest/test").status_code == 429                    # nicht im Sekundentakt


# ---------- Zeitplan und Doppelversand ----------

def test_sends_once_per_week_despite_restarts(admin, smtp, fake_smtp):
    _enable(admin, weekday=0)
    assert digest.run_due(MONDAY.replace(hour=6, minute=59)) == 0           # vor 7 Uhr nichts
    assert digest.run_due(MONDAY) == 1
    # „Neustarts“ und weitere Durchläufe am selben Tag und in derselben Woche: nichts mehr
    for later in (MONDAY.replace(hour=8, minute=5), MONDAY.replace(hour=23), MONDAY + dt.timedelta(days=1)):
        assert digest.run_due(later) == 0
    assert len(fake_smtp.sent) == 1
    # Wochentag mitten in der Woche umstellen: diese Woche nicht noch einmal
    admin.put("/digest", json={"weekday": 2})
    assert digest.run_due(MONDAY + dt.timedelta(days=2)) == 0
    # nächste Woche wieder – genau einmal
    assert digest.run_due(MONDAY + dt.timedelta(days=9)) == 1
    assert digest.run_due(MONDAY + dt.timedelta(days=9, hours=1)) == 0
    assert len(fake_smtp.sent) == 2
    d = admin.get("/digest").json()
    assert d["last_sent"].startswith("2026-12-02") and d["last_error"] is None


def test_missed_slot_is_caught_up_same_day_only(admin, smtp, fake_smtp):
    _enable(admin, weekday=0)
    # Server war Montag aus – Dienstag kommt nichts nach
    assert digest.run_due(MONDAY + dt.timedelta(days=1)) == 0
    # in der Woche darauf startet er erst am Nachmittag: dann einmal nachholen
    assert digest.run_due(MONDAY + dt.timedelta(days=7, hours=8)) == 1
    assert len(fake_smtp.sent) == 1


def test_claim_blocks_second_runner(admin, smtp, fake_smtp):
    """Ein zweiter Durchlauf, der die Woche schon vergeben findet (anderer Prozess), sendet nicht."""
    _enable(admin, weekday=0)
    with connect() as con:
        con.execute("UPDATE digest_prefs SET last_week=? WHERE user_id=?", (digest.week_key(MONDAY.date()), admin.id))
    assert digest.run_due(MONDAY) == 0 and fake_smtp.sent == []


def test_failure_releases_week_and_retries_later_same_day(admin, smtp, fake_smtp):
    _enable(admin, weekday=0)
    fake_smtp.fail = True
    assert digest.run_due(MONDAY) == 0
    d = admin.get("/digest").json()
    assert "Verbindung abgelehnt" in d["last_error"]
    fake_smtp.fail = False
    assert digest.run_due(MONDAY + dt.timedelta(minutes=10)) == 0          # nicht gleich wieder
    assert digest.run_due(MONDAY + dt.timedelta(minutes=31)) == 1
    assert digest.run_due(MONDAY + dt.timedelta(minutes=40)) == 0
    assert len(fake_smtp.sent) == 1
    assert admin.get("/digest").json()["last_error"] is None


def test_disabled_or_other_weekday_gets_nothing(admin, sam, smtp, fake_smtp):
    _enable(admin, weekday=3)
    sam.put("/digest", json={"email": "sam@example.org", "weekday": 0})     # Adresse, aber aus
    assert digest.run_due(MONDAY) == 0 and fake_smtp.sent == []


def test_demo_sends_nothing(admin, smtp, fake_smtp, monkeypatch):
    _enable(admin, weekday=0)
    monkeypatch.setattr(config, "DEMO", True)
    assert digest.run_due(MONDAY) == 0 and fake_smtp.sent == []


# ---------- Inhalt ----------

def _weekly(api, fake_smtp, now=MONDAY):
    assert digest.run_due(now) >= 1
    msg = [m for m in fake_smtp.sent if m["To"] == api.user["username"] + "@example.org"][-1]
    return _text(msg), _html(msg), msg


def test_content_due_upcoming_new_review_and_links(admin, sam, smtp, fake_smtp):
    with connect() as con:
        con.execute("INSERT INTO settings(key, value) VALUES ('public_url', 'https://papigator.example.org')")
    today = dt.date.today()
    late = admin.upload("Kfz-Steuer Bescheid", title="Kfz-Steuer").json()["id"]
    soon = admin.upload("Strom Abschlag", title="Stromvertrag kündigen").json()["id"]
    far = admin.upload("Viel später", title="Ganz weit weg").json()["id"]
    pass_ = admin.upload("Reisepass", title="Reisepass Alex").json()["id"]
    admin.patch(f"/documents/{late}", json={"due_date": (today - dt.timedelta(days=3)).isoformat(), "due_note": "Zahlen",
                                            "sender": "Hauptzollamt"})
    admin.patch(f"/documents/{soon}", json={"due_date": (today + dt.timedelta(days=5)).isoformat()})
    admin.patch(f"/documents/{far}", json={"due_date": (today + dt.timedelta(days=200)).isoformat()})
    with connect() as con:
        con.execute("UPDATE documents SET expires_on=?, expires_what='Reisepass' WHERE id=?",
                    ((today + dt.timedelta(days=40)).isoformat(), pass_))
    _enable(admin, weekday=MONDAY.weekday(), addr="alex@example.org")
    now = dt.datetime.combine(today, dt.time(8, 0))
    with connect() as con:
        con.execute("UPDATE digest_prefs SET weekday=?", (now.weekday(),))
    text, page, msg = _weekly(admin, fake_smtp, now)
    assert "KW" in msg["Subject"] and "Kfz" not in msg["Subject"]
    assert "Hallo Alex" in text
    assert "[!] Kfz-Steuer" in text and "überfällig" in text and "Hauptzollamt" in text
    assert "Zahlen" not in text                                            # Notiz zur Frist bleibt draußen
    assert "Stromvertrag kündigen" in text
    due_part = text.split("Neu in den letzten 7 Tagen")[0]
    assert "Ganz weit weg" not in due_part                                 # mehr als 30 Tage voraus
    assert due_part.index("Kfz-Steuer") < due_part.index("Stromvertrag")    # Überfälliges zuerst
    assert "Im Blick" in text and "Reisepass" in text
    assert "4 neue Dokumente" in text
    assert "Zu prüfen" in text and "warten darauf" in text                  # ohne Tags = zu prüfen
    assert f"https://papigator.example.org/app/#doc-{late}" in page and "https://papigator.example.org/app/#review" in page
    assert "https://papigator.example.org/app/" in text


def test_no_links_without_public_address(admin, smtp, fake_smtp):
    admin.upload("x", title="Brief")
    admin.put("/digest", json={"email": "alex@example.org"})
    assert admin.get("/digest").json()["links"] is False
    admin.post("/digest/test")
    msg = fake_smtp.sent[-1]
    assert "/app/#doc-" not in _html(msg) and "http" not in _text(msg)


def test_html_escapes_titles(admin, smtp, fake_smtp):
    admin.upload("x", title='<script>alert(1)</script> & "Brief"')
    admin.put("/digest", json={"email": "alex@example.org"})
    admin.post("/digest/test")
    page = _html(fake_smtp.sent[-1])
    assert "<script>" not in page and "&lt;script&gt;" in page


def test_only_what_the_account_may_see_and_no_money(admin, make_user, smtp, fake_smtp):
    kim, sam = make_user("kim"), make_user("sam")
    secret = sam.upload("Geheimes von Sam", title="Sams Geheimbrief").json()["id"]
    sam.patch(f"/documents/{secret}", json={"due_date": dt.date.today().isoformat()})
    bill = kim.upload("Rechnung Elektro", title="Rechnung Waschmaschine").json()["id"]
    kim.patch(f"/documents/{bill}", json={"amount": 1234.56, "currency": "EUR", "due_note": "987,65 überweisen",
                                          "due_date": (dt.date.today() + dt.timedelta(days=2)).isoformat()})
    with connect() as con:   # offene Erstattung (Geld) und eine Preiserhöhung dürfen nicht in die Mail
        con.execute("UPDATE documents SET refund_state='open', document_date=? WHERE id=?", (dt.date.today().isoformat(), bill))
    csv_ = "Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag\n01.11.2026;Supermarkt Beispiel;Einkauf;-77,77\n"
    assert kim.post("/bank/import", files={"file": ("x.csv", csv_.encode(), "text/csv")},
                    data={"account_name": "Giro"}).status_code == 200
    kim.put("/digest", json={"email": "kim@example.org"})
    assert kim.post("/digest/test").status_code == 200
    text, page = _text(fake_smtp.sent[-1]), _html(fake_smtp.sent[-1])
    assert "Rechnung Waschmaschine" in text
    for leak in ("Sams Geheimbrief", "Geheimes", "1234", "1.234", "€", "987", "überweisen", "77,77", "Supermarkt", "Giro", "/s/"):
        assert leak not in text and leak not in page, leak
    assert "Arztrechnung" not in text and "Erstattung" not in text


def test_partner_documents_follow_overview_setting_and_assignment(admin, make_user, smtp, fake_smtp):
    kim, sam = make_user("kim"), make_user("sam")
    assert sam.put("/shares", json={"user_id": kim.id, "can_edit": True}).status_code == 200
    a = sam.upload("Sam 1", title="Sams Frist allgemein").json()["id"]
    b = sam.upload("Sam 2", title="Sams Frist für Kim").json()["id"]
    soon = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    sam.patch(f"/documents/{a}", json={"due_date": soon})
    sam.patch(f"/documents/{b}", json={"due_date": soon, "due_assignee": kim.id})
    kim.put("/digest", json={"email": "kim@example.org"})
    kim.post("/digest/test")
    text = _text(fake_smtp.sent[-1])
    # Übersicht nur „Meine“ (Standard): Sams Dokumente fehlen – außer der Frist, um die ich mich kümmere
    assert "Sams Frist für Kim" in text and "Sams Frist allgemein" not in text
    kim.patch("/auth/me", json={"show_partner_in_overview": True})
    digest._last_test.clear()
    kim.post("/digest/test")
    text = _text(fake_smtp.sent[-1])
    assert "Sams Frist allgemein" in text and "Sams Frist für Kim" in text


def test_others_assignment_listed_after_mine(admin, make_user, smtp, fake_smtp):
    kim = make_user("kim")
    assert admin.put("/shares", json={"user_id": kim.id, "can_edit": True}).status_code == 200
    admin.patch("/auth/me", json={"show_partner_in_overview": True})
    x = admin.upload("x", title="Frist für Kim").json()["id"]
    y = admin.upload("y", title="Meine Frist").json()["id"]
    d = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    admin.patch(f"/documents/{x}", json={"due_date": d, "due_assignee": kim.id})
    admin.patch(f"/documents/{y}", json={"due_date": (dt.date.today() + dt.timedelta(days=9)).isoformat()})
    admin.put("/digest", json={"email": "alex@example.org"})
    admin.post("/digest/test")
    text = _text(fake_smtp.sent[-1])
    assert text.index("Meine Frist") < text.index("Kümmern sich andere") < text.index("Frist für Kim")
    assert "kümmert sich: kim" in text


def test_account_deletion_removes_prefs(admin, make_user, smtp):
    kim = make_user("kim")
    kim.put("/digest", json={"enabled": True, "email": "kim@example.org"})
    from conftest import PASSWORD
    assert kim.client.request("DELETE", "/auth/me", json={"password": PASSWORD},
                              headers={"Authorization": f"Bearer {kim.token}"}).status_code == 204
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM digest_prefs WHERE user_id=?", (kim.id,)).fetchone()[0] == 0


# ---------- Sprache (25.11.2026) ----------

def test_english_by_account_language(admin, sam, smtp, fake_smtp):
    """Web/App schicken ihre Oberflächensprache mit: Alex bekommt Englisch, Sam (ohne Angabe) die Server-Sprache."""
    pass_ = admin.upload("Reisepass", title="Passport Alex").json()["id"]
    with connect() as con:
        con.execute("UPDATE documents SET expires_on=?, expires_what='Reisepass' WHERE id=?",
                    ((MONDAY.date() + dt.timedelta(days=40)).isoformat(), pass_))
    r = admin.put("/digest", json={"enabled": True, "weekday": MONDAY.weekday(), "email": "alex@example.org",
                                   "lang": "en"})
    assert r.json()["lang"] == "en" and r.json()["weekdays"][0] == "Monday"
    _enable(sam, weekday=MONDAY.weekday(), addr="sam@example.org")
    assert sam.get("/digest").json()["lang"] == "de"
    # Speichern ohne lang lässt die Sprache, wie sie ist
    admin.put("/digest", json={"weekday": MONDAY.weekday()})
    assert admin.get("/digest").json()["lang"] == "en"
    assert admin.get("/digest?lang=de").json()["weekdays"][0] == "Montag"   # Oberfläche deutsch, Mail englisch
    assert digest.run_due(MONDAY) == 2
    by_to = {m["To"]: m for m in fake_smtp.sent}
    msg = by_to["alex@example.org"]
    text, page = _text(msg), _html(msg)
    assert msg["Subject"] == "Papigator – your week (week 48)"
    assert "Hi Alex," in text and "New in the last 7 days" in text and "(Monday)" in text
    assert "Coming up" in text and "(Passport, expires " in text and "Reisepass" not in text
    assert "Hallo" not in text and "Wochenübersicht" not in text
    assert '<html lang="en">' in page
    smsg = by_to["sam@example.org"]
    assert "KW 48" in smsg["Subject"] and "Hallo" in _text(smsg)


def test_server_language_is_the_default(admin, smtp, fake_smtp):
    admin.patch("/settings", json={"llm_language": "en"})
    _enable(admin, weekday=MONDAY.weekday(), addr="admin@example.org")
    today = dt.date.today()
    due = (today + dt.timedelta(days=3)).isoformat()
    d = admin.upload("Rechnung", title="Telekom").json()["id"]
    admin.patch(f"/documents/{d}", json={"due_date": due})
    now = dt.datetime.combine(today, dt.time(8, 0))
    with connect() as con:
        con.execute("UPDATE digest_prefs SET weekday=?", (now.weekday(),))
    assert digest.run_due(now) == 1
    msg = fake_smtp.sent[-1]
    text = _text(msg)
    assert "your week" in msg["Subject"] and "Open deadlines" in text
    assert f"due {digest._fmt_day(due, 'en')}" in text and digest._fmt_day("2026-01-08", "en") == "Jan 8, 2026"
    # "" = zurück auf die Server-Sprache
    admin.put("/digest", json={"lang": ""})
    assert admin.get("/digest").json()["lang"] == "en"
    admin.patch("/settings", json={"llm_language": "de"})
    assert admin.get("/digest").json()["lang"] == "de"
