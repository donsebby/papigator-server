"""Wochenübersicht per Mail (25.11.2026): einmal pro Woche, für jedes Konto, das sie einschaltet.

Inhalt – nur, was das Konto auch in den Listen sieht (gleiche Besitzer-Auswahl wie /documents, /upcoming,
/documents/review_count mit dem Standard-Bereich des Kontos; Fristen, die mir zugeteilt sind, kommen immer dazu):
  - offene Fristen (überfällig und die nächsten 30 Tage), mir zugeteilte oder niemandem zugeteilte zuerst
    (wie `mine_due`, `due.MINE_DUE_SQL`), Überfällige markiert,
  - „Im Blick“ (`extras.upcoming`) ohne Geld: nur Ablaufdaten, Gewährleistung/Garantie, Prüfungen/Wartungen –
    keine Erstattungen, keine Preiserhöhungen, keine Beträge,
  - neue Dokumente der letzten 7 Tage (Anzahl, bis zu 10 Titel mit Absender und Datum),
  - Anzahl „Zu prüfen“.
Nie: Kontoumsätze, Beträge, Freigabe-Links, Dokumenttext. Links führen in die Web-App unter der öffentlichen
Adresse (Einstellung „Öffentliche Adresse“, sonst PAPIGATOR_PUBLIC_URL) – ohne die gibt es keine Links.

Versand über SMTP (Admin: Einstellungen → Server → Mail-Versand; Passwort nur verschlüsselt). Ohne SMTP wird nichts
gesendet. Zeitplan: am gewählten Wochentag ab SEND_HOUR Serverzeit. Doppelversand verhindert `digest_prefs.last_week`
(ISO-Jahr-Woche): vor dem Senden per bedingtem UPDATE beansprucht – zwei Durchläufe (Neustart, zweiter Prozess)
können dieselbe Woche nicht beide bekommen. Schlägt der Versand fehl, wird die Woche wieder freigegeben und frühestens
nach RETRY_MINUTES erneut versucht – nur am selben Tag. War der Server zur Uhrzeit aus, kommt die Mail einmal, sobald
er am selben Tag wieder läuft; am nächsten Tag nicht mehr (dann erst wieder in der Woche darauf).
In der Demo (PAPIGATOR_DEMO) läuft nichts davon, die Routen sind gesperrt (app/demo.py).
"""
import datetime as dt
import html
import logging
import re
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import auth, config, extras, llm, security
from . import due as duemod
from .auth import User, current_user, require_admin
from .db import connect, get_setting, set_setting

log = logging.getLogger("papigator.digest")
router = APIRouter()

SEND_HOUR = 7            # morgens, Serverzeit
RETRY_MINUTES = 30       # nach einem Fehlschlag frühestens wieder
DUE_DAYS = 30            # Fristen bis so weit voraus
MAX_DUE = 15
MAX_NEW = 10
LOOP_SECONDS = 300

# ---------- Texte (an einer Stelle) ----------
# Sprache pro Konto (digest_prefs.lang, von Web/App beim Speichern mitgeschickt), sonst die des Servers (config.language).
# T bleibt das deutsche Wörterbuch (auch für die Fehlertexte der Routen), T_EN hat nur die Texte der Mail.

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
T = {
    "subject": "Papigator – deine Woche (KW {week})",
    "hello": "Hallo {name},",
    "intro": "hier ist deine Wochenübersicht aus Papigator.",
    "due": "Offene Fristen",
    "due_none": "Keine offenen Fristen in den nächsten {days} Tagen.",
    "due_mine": "Für dich oder noch niemandem zugeteilt",
    "due_others": "Kümmern sich andere",
    "overdue": "überfällig",
    "due_on": "fällig {date}",
    "due_who": "kümmert sich: {name}",
    "due_more": "… und {n} weitere",
    "soon": "Im Blick",
    "new": "Neu in den letzten 7 Tagen",
    "new_count": "{n} neue Dokumente",
    "new_count_1": "1 neues Dokument",
    "new_none": "Keine neuen Dokumente.",
    "new_more": "… und {n} weitere",
    "review": "Zu prüfen",
    "review_count": "{n} Dokumente warten darauf, dass du sie prüfst.",
    "review_count_1": "1 Dokument wartet darauf, dass du es prüfst.",
    "review_none": "Nichts zu prüfen.",
    "open_app": "Papigator öffnen",
    "open_review": "Zu prüfen öffnen",
    "footer": "Diese Mail kommt einmal pro Woche ({weekday}). Abschalten: Papigator → Einstellungen → Wochenübersicht.",
    "test_note": "Test-Mail – so sieht deine Wochenübersicht aus.",
    "expiry": "läuft ab",
    "warranty_until": "endet",
    "service_due": "fällig",
    "no_title": "Dokument",
    # Fehler und Hinweise für die Einstellungen
    "err_no_smtp": "Der Mail-Versand ist auf diesem Server nicht eingerichtet – ein Admin trägt ihn unter Einstellungen → Server → Mail-Versand ein.",
    "err_no_email": "Erst eine Empfängeradresse eintragen",
    "err_email": "Ungültige Mail-Adresse",
    "err_weekday": "Wochentag 0 (Montag) bis 6 (Sonntag)",
    "err_bootstrap": "Erst anmelden",
    "err_wait": "Bitte einen Moment warten, bevor du die nächste Test-Mail schickst",
    "err_smtp_fields": "Server und Absenderadresse angeben",
    "err_plain_login": "Anmelden außerhalb des Heimnetzes nur verschlüsselt (STARTTLS oder SSL) – sonst ginge das Passwort im Klartext übers Netz.",
}

T_EN = {
    "subject": "Papigator – your week (week {week})",
    "hello": "Hi {name},",
    "intro": "here is your weekly summary from Papigator.",
    "due": "Open deadlines",
    "due_none": "No open deadlines in the next {days} days.",
    "due_mine": "For you or not assigned yet",
    "due_others": "Others are taking care of these",
    "overdue": "overdue",
    "due_on": "due {date}",
    "due_who": "taken care of by: {name}",
    "due_more": "… and {n} more",
    "soon": "Coming up",
    "new": "New in the last 7 days",
    "new_count": "{n} new documents",
    "new_count_1": "1 new document",
    "new_none": "No new documents.",
    "new_more": "… and {n} more",
    "review": "To review",
    "review_count": "{n} documents are waiting for you to review them.",
    "review_count_1": "1 document is waiting for you to review it.",
    "review_none": "Nothing to review.",
    "open_app": "Open Papigator",
    "open_review": "Open “To review”",
    "footer": "This mail comes once a week ({weekday}). To turn it off: Papigator → Settings → Weekly summary.",
    "test_note": "Test mail – this is what your weekly summary looks like.",
    "expiry": "expires",
    "warranty_until": "ends",
    "service_due": "due",
    "no_title": "Document",
}
# „Im Blick“: die Arten bleiben in der DB deutsch (processing.EXPIRES/SERVICES, extras.upcoming) – für die Mail übersetzt
WHAT_EN = {
    "Personalausweis": "ID card", "Reisepass": "Passport", "Führerschein": "Driving licence",
    "Aufenthaltstitel": "Residence permit", "Bankkarte": "Bank card", "Kreditkarte": "Credit card",
    "Gesundheitskarte": "Health insurance card", "Schwerbehindertenausweis": "Disability ID", "Parkausweis": "Parking permit",
    "BahnCard": "BahnCard", "Hauptuntersuchung": "Vehicle inspection", "Heizungswartung": "Heating service",
    "Schornsteinfeger": "Chimney sweep", "Rauchmelder": "Smoke detectors", "Garantie": "Guarantee",
    "Gewährleistung": "Statutory warranty",
}
LANGS = ("de", "en")


def texts(lang: str) -> dict:
    return {**T, **T_EN} if lang == "en" else T


def weekdays(lang: str) -> list[str]:
    return WEEKDAYS_EN if lang == "en" else WEEKDAYS


def _lang(con, stored: str | None) -> str:
    return stored if stored in LANGS else config.language(con)


# ---------- SMTP-Einstellungen (Admin) ----------

SMTP_DEFAULTS = {"smtp_host": "", "smtp_port": "587", "smtp_security": "starttls", "smtp_user": "", "smtp_password": "",
                 "smtp_from": ""}
SECURITY = ("starttls", "ssl", "none")
_EMAIL = re.compile(r"^[^@\s<>,;\"'()\[\]\\]+@[^@\s<>,;\"'()\[\]\\]+\.[^@\s<>,;\"'()\[\]\\]+$")


def valid_email(s: str) -> bool:
    return bool(s) and len(s) <= 254 and "\r" not in s and "\n" not in s and bool(_EMAIL.fullmatch(s))


def _base(host: str | None) -> str:
    """„smtp.mail.example.org“ → „example.org“ (grob, für den Vergleich mit dem IMAP-Server)."""
    parts = [p for p in str(host or "").lower().strip().strip(".").split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else ""


def _imap_fallback(con, s: dict) -> None:
    """Ohne eigenes Passwort: dasselbe Konto wie der E-Mail-Eingang → dessen Passwort (wie beim Anbieter üblich)."""
    s["smtp_password_from_imap"] = False
    # nur beim selben Anbieter (gleiche Domain wie der IMAP-Server) – sonst ginge das IMAP-Passwort an einen fremden Host
    same = _base(s.get("smtp_host")) and _base(s.get("smtp_host")) == _base(get_setting(con, "mail_host", "") or "")
    if same and not s["smtp_password"] and s["smtp_user"] and s["smtp_user"] == (get_setting(con, "mail_user", "") or ""):
        imap_pw = security.decrypt(get_setting(con, "mail_password", "") or "") or ""
        if imap_pw:
            s["smtp_password"] = imap_pw
            s["smtp_password_from_imap"] = True


def _stored(con) -> dict:
    s = {k: get_setting(con, k, v) or v for k, v in SMTP_DEFAULTS.items()}
    s["smtp_password"] = security.decrypt(s["smtp_password"]) or ""
    return s


def smtp_settings(with_password: bool = False) -> dict:
    with connect() as con:
        s = _stored(con)
        _imap_fallback(con, s)
        s["smtp_password_set"] = bool(s["smtp_password"])
        s["configured"] = bool(s["smtp_host"] and s["smtp_from"])
        if not with_password:
            s.pop("smtp_password")
        return s


def suggestion() -> dict:
    """Vorschlag aus dem E-Mail-Eingang (IMAP): imap.… → smtp.…, Port 587 mit STARTTLS, derselbe Benutzer."""
    with connect() as con:
        host = (get_setting(con, "mail_host", "") or "").strip()
        user = (get_setting(con, "mail_user", "") or "").strip()
    if not host:
        return {}
    smtp_host = re.sub(r"^(imap|mail\.imap|imap4)\.", "smtp.", host, flags=re.I)
    if smtp_host == host and not host.lower().startswith(("smtp.", "mail.")):
        smtp_host = "smtp." + host.split(".", 1)[-1] if host.count(".") >= 2 else host
    return {"smtp_host": smtp_host, "smtp_port": "587", "smtp_security": "starttls", "smtp_user": user,
            "smtp_from": user if valid_email(user) else ""}


def _open(s: dict) -> smtplib.SMTP:
    """Verbinden und – mit Benutzer und Passwort – anmelden; s["logged_in"] sagt, ob angemeldet wurde."""
    port = int(s["smtp_port"] or 587)
    if s["smtp_security"] == "ssl":
        box = smtplib.SMTP_SSL(s["smtp_host"], port, timeout=30, context=ssl.create_default_context())
    else:
        box = smtplib.SMTP(s["smtp_host"], port, timeout=30)
        if s["smtp_security"] == "starttls":
            box.starttls(context=ssl.create_default_context())
    s["logged_in"] = bool(s["smtp_user"] and s.get("smtp_password"))
    if s["logged_in"] and s["smtp_security"] == "none" and not llm.is_local("smtp://" + s["smtp_host"]):
        try:
            box.close()
        except Exception:
            pass
        raise RuntimeError(T["err_plain_login"])   # Passwort nie im Klartext an einen Server außerhalb des Heimnetzes
    if s["logged_in"]:
        box.login(s["smtp_user"], s["smtp_password"])
    return box


def smtp_test(override: dict | None = None) -> dict:
    """Verbinden und anmelden, ohne etwas zu senden; override = Werte aus dem noch ungespeicherten Formular."""
    with connect() as con:
        s = _stored(con)
        stored = dict(s)
        for k, v in (override or {}).items():
            if k in SMTP_DEFAULTS and v not in (None, ""):
                s[k] = str(v)
        # Ein gespeichertes Passwort geht nur an den gespeicherten Server: wer Host, Port oder Benutzer im Formular
        # ändert, muss das Passwort mitschicken – sonst ließe es sich an einen fremden Server schicken
        changed = any(str(s.get(k) or "") != str(stored.get(k) or "") for k in ("smtp_host", "smtp_port", "smtp_user"))
        if changed and not (override or {}).get("smtp_password"):
            s["smtp_password"] = ""
        _imap_fallback(con, s)   # nach den Formularwerten: der Benutzer kann gerade erst übernommen worden sein
    if not s["smtp_host"]:
        return {"ok": False, "error": T["err_smtp_fields"]}
    try:
        box = _open(s)
        box.noop()
        box.quit()
        return {"ok": True, "login": s["logged_in"]}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


def send(to: str, subject: str, text: str, html_body: str) -> None:
    s = smtp_settings(with_password=True)
    if not s["configured"]:
        raise RuntimeError(T["err_no_smtp"])
    if not valid_email(to) or not valid_email(s["smtp_from"]):
        raise ValueError(T["err_email"])
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Papigator", s["smtp_from"]))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=s["smtp_from"].rsplit("@", 1)[-1])
    msg["Auto-Submitted"] = "auto-generated"   # keine Abwesenheitsnotizen zurück
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    box = _open(s)
    try:
        box.send_message(msg)
    finally:
        try:
            box.quit()
        except Exception:
            pass


# ---------- Inhalt ----------

def public_base(con) -> str:
    return (get_setting(con, "public_url") or config.PUBLIC_URL or "").rstrip("/")


def _fmt_day(day: str | None, lang: str = "de") -> str:
    """Datum für die Mail: 08.01.2026 bzw. Jan 8, 2026 (ohne Gebietsschema des Servers, eindeutig in beiden Ländern)."""
    if not day:
        return ""
    try:
        d = dt.date.fromisoformat(day[:10])
    except ValueError:
        return ""
    return f"{_MONTHS_EN[d.month - 1]} {d.day}, {d.year}" if lang == "en" else d.strftime("%d.%m.%Y")


def build(con, user: User, now: dt.datetime, lang: str = "de") -> dict:
    """Bausteine der Mail – nur Titel, Absender, Daten und Zahlen; keine Beträge, kein Text."""
    T = texts(lang)
    today = now.date()
    scope = "all" if user.show_partner_in_overview else "mine"
    owners = auth.visible_owner_ids(con, user, scope)
    seen = auth.visible_owner_ids(con, user, "all")       # alles, was ich sehen darf (für mir zugeteilte Fristen)
    out = {"due_mine": [], "due_others": [], "due_more": 0, "soon": [], "new": [], "new_count": 0, "review": 0}
    if seen:
        m_all = ",".join("?" * len(seen))
        m_own = ",".join("?" * len(owners)) or "NULL"
        until = (today + dt.timedelta(days=DUE_DAYS)).isoformat()
        rows = con.execute(
            f"SELECT d.*, CASE WHEN {duemod.MINE_DUE_SQL} THEN 0 ELSE 1 END AS grp FROM documents d "
            f"WHERE d.deleted_at IS NULL AND d.ext_pending=0 AND d.due_date IS NOT NULL AND COALESCE(d.due_done, 0)=0 "
            f"AND substr(d.due_date, 1, 10) <= ? AND d.owner_id IN ({m_all}) "
            f"AND (d.owner_id IN ({m_own}) OR d.due_assignee = ?) "
            f"ORDER BY grp, d.due_date, d.id",
            (user.id, until, *seen, *owners, user.id)).fetchall()
        shown = 0
        for r in rows:
            if shown >= MAX_DUE:
                out["due_more"] += 1
                continue
            shown += 1
            who = duemod.assignee_fields(con, user, r)
            item = {"id": r["id"], "title": r["title"] or T["no_title"], "sender": r["sender"],
                    "date": r["due_date"][:10], "overdue": r["due_date"][:10] < today.isoformat(),
                    "who": who["due_assignee_name"] if who["due_assignee"] not in (None, user.id) else None}
            out["due_mine" if r["grp"] == 0 else "due_others"].append(item)
    if owners:
        marks = ",".join("?" * len(owners))
        labels = {"expiry": T["expiry"], "warranty": T["warranty_until"], "service": T["service_due"]}
        for i in extras.upcoming(con, owners, today):
            if i["type"] not in labels:      # Erstattungen und Preiserhöhungen sind Geld – nicht in die Mail
                continue
            what = i.get("what")
            out["soon"].append({"id": i["document_id"], "title": i["title"] or T["no_title"],
                                "what": WHAT_EN.get(what, what) if lang == "en" else what,
                                "date": i["date"], "label": labels[i["type"]]})
        cutoff = (now.astimezone(dt.timezone.utc) - dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        base = (f"FROM documents d WHERE d.deleted_at IS NULL AND d.ext_pending=0 AND d.owner_id IN ({marks}) "
                f"AND d.created_at >= ?")
        out["new_count"] = con.execute(f"SELECT COUNT(*) {base}", (*owners, cutoff)).fetchone()[0]
        out["new"] = [{"id": r["id"], "title": r["title"] or T["no_title"], "sender": r["sender"], "date": r["document_date"]}
                      for r in con.execute(f"SELECT d.id, d.title, d.sender, d.document_date {base} "
                                           f"ORDER BY d.created_at DESC, d.id DESC LIMIT ?", (*owners, cutoff, MAX_NEW))]
        from .main import REVIEW_SQL   # späte Bindung: main importiert dieses Modul
        out["review"] = con.execute(f"SELECT COUNT(*) FROM documents d WHERE d.deleted_at IS NULL AND d.owner_id IN "
                                    f"({marks}) AND {REVIEW_SQL}", owners).fetchone()[0]
    return out


def render(data: dict, name: str, base: str, weekday: int, now: dt.datetime, test: bool = False,
           lang: str = "de") -> tuple[str, str, str]:
    """(Betreff, Text, HTML). Alles aus der Datenbank wird fürs HTML maskiert."""
    T = texts(lang)

    def _de(day):
        return _fmt_day(day, lang)
    e = html.escape
    subject = T["subject"].format(week=now.isocalendar()[1])
    text: list[str] = []
    body: list[str] = []
    link_style = 'style="color:#1f6f43"'

    def h2(title: str):
        text.extend(["", title, "-" * len(title)])
        body.append(f'<h2 style="font-size:17px;margin:22px 0 6px">{e(title)}</h2>')

    def para(s: str, muted: bool = False):
        text.append(s)
        body.append(f'<p style="color:#666">{e(s)}</p>' if muted else f"<p>{e(s)}</p>")

    def items(rows: list[dict], sub: str | None = None):
        if sub:
            text.append(sub + ":")
            body.append(f'<p style="margin:8px 0 2px;color:#444"><b>{e(sub)}</b></p>')
        lis = []
        for i in rows:
            parts = [x for x in i["parts"] if x]
            mark = "[!] " if i.get("alert") else ""
            text.append(f"- {mark}{i['title']}" + (f" ({', '.join(parts)})" if parts else ""))
            link = f"{base}/app/#doc-{i['id']}" if base and i.get("id") else None
            title = f'<a href="{e(link)}" {link_style}>{e(i["title"])}</a>' if link else e(i["title"])
            alert = '<b style="color:#b3261e">!</b> ' if i.get("alert") else ""
            extra = f' <span style="color:#666">· {e(" · ".join(parts))}</span>' if parts else ""
            lis.append(f"<li>{alert}{title}{extra}</li>")
        body.append('<ul style="margin:4px 0 8px;padding-left:20px">' + "".join(lis) + "</ul>")

    if test:
        text.extend([T["test_note"], ""])
        body.append(f'<p style="background:#fff6d6;padding:8px 12px;border-radius:6px">{e(T["test_note"])}</p>')
    text.extend([T["hello"].format(name=name), T["intro"]])
    body.append(f"<p>{e(T['hello'].format(name=name))}<br>{e(T['intro'])}</p>")

    # Fristen
    h2(T["due"])

    def due(i):
        when = T["due_on"].format(date=_de(i["date"]))
        return {"id": i["id"], "title": i["title"], "alert": i["overdue"],
                "parts": [f"{T['overdue']}, {when}" if i["overdue"] else when, i.get("sender"),
                          T["due_who"].format(name=i["who"]) if i.get("who") else None]}
    if data["due_mine"]:
        items([due(i) for i in data["due_mine"]], T["due_mine"] if data["due_others"] else None)
    if data["due_others"]:
        items([due(i) for i in data["due_others"]], T["due_others"])
    if data["due_more"]:
        para(T["due_more"].format(n=data["due_more"]), muted=True)
    if not (data["due_mine"] or data["due_others"]):
        para(T["due_none"].format(days=DUE_DAYS), muted=True)

    # Im Blick (ohne Geld)
    if data["soon"]:
        h2(T["soon"])
        items([{"id": i["id"], "title": i["title"], "parts": [i.get("what"), f"{i['label']} {_de(i['date'])}".strip()]}
               for i in data["soon"]])

    # Neu
    h2(T["new"])
    n = data["new_count"]
    if n:
        para(T["new_count_1"] if n == 1 else T["new_count"].format(n=n))
        items([{"id": i["id"], "title": i["title"], "parts": [i.get("sender"), _de(i.get("date"))]} for i in data["new"]])
        if n > len(data["new"]):
            para(T["new_more"].format(n=n - len(data["new"])), muted=True)
    else:
        para(T["new_none"], muted=True)

    # Zu prüfen
    h2(T["review"])
    r = data["review"]
    para(T["review_none"] if not r else (T["review_count_1"] if r == 1 else T["review_count"].format(n=r)), muted=not r)
    if r and base:
        text.append(f"{T['open_review']}: {base}/app/#review")
        body.append(f'<p><a href="{e(base)}/app/#review" {link_style}>{e(T["open_review"])}</a></p>')

    if base:
        text.extend(["", f"{T['open_app']}: {base}/app/"])
        body.append(f'<p style="margin-top:22px"><a href="{e(base)}/app/" style="display:inline-block;background:#1f6f43;'
                    f'color:#fff;padding:9px 16px;border-radius:8px;text-decoration:none">{e(T["open_app"])}</a></p>')
    foot = T["footer"].format(weekday=weekdays(lang)[weekday % 7])
    text.extend(["", "-- ", foot])
    body.append(f'<p style="margin-top:28px;color:#888;font-size:12px">{e(foot)}</p>')
    page = (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<title>{e(subject)}</title></head><body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
            'font-size:15px;line-height:1.45;color:#222;max-width:640px;margin:0 auto;padding:16px">'
            + "".join(body) + "</body></html>")
    return subject, "\n".join(text).strip() + "\n", page


def compose(con, user_id: int, now: dt.datetime, test: bool = False) -> Optional[tuple[str, str, str, str]]:
    """(Empfänger, Betreff, Text, HTML) oder None (Konto weg, keine Adresse)."""
    r = con.execute("SELECT u.*, p.email, p.weekday, p.lang AS digest_lang FROM users u JOIN digest_prefs p ON p.user_id=u.id WHERE u.id=?",
                    (user_id,)).fetchone()
    if not r or not r["email"]:
        return None
    user = User.from_row(r)
    first = ((r["display_name"] or "").strip().split() or [r["username"]])[0]   # nur der Vorname, wie in „Heute“
    lang = _lang(con, r["digest_lang"])
    subject, text, page = render(build(con, user, now, lang), first, public_base(con), r["weekday"], now, test, lang)
    return r["email"], subject, text, page


# ---------- Zeitplan ----------

def week_key(day: dt.date) -> str:
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def run_due(now: Optional[dt.datetime] = None) -> int:
    """Alle fälligen Wochenübersichten senden; gibt die Zahl der gesendeten zurück. Mehrfach aufrufbar."""
    if config.DEMO:
        return 0
    now = now or dt.datetime.now()
    if now.hour < SEND_HOUR:
        return 0
    if not smtp_settings()["configured"]:
        return 0
    key = week_key(now.date())
    retry_before = (now - dt.timedelta(minutes=RETRY_MINUTES)).isoformat(timespec="seconds")
    sent = 0
    with connect() as con:
        due = [r["user_id"] for r in con.execute(
            "SELECT user_id FROM digest_prefs WHERE enabled=1 AND email != '' AND weekday=? "
            "AND (last_week IS NULL OR last_week != ?) "
            "AND (last_try IS NULL OR last_try < ? OR substr(last_try, 1, 10) != ?)",
            (now.weekday(), key, retry_before, now.date().isoformat()))]
    for uid in due:
        with connect() as con:
            prev = con.execute("SELECT last_week FROM digest_prefs WHERE user_id=?", (uid,)).fetchone()
            # Beanspruchen: nur wer die Woche hier setzt, sendet – ein zweiter Durchlauf findet sie schon vergeben
            claimed = con.execute("UPDATE digest_prefs SET last_week=?, last_try=? WHERE user_id=? AND enabled=1 "
                                  "AND (last_week IS NULL OR last_week != ?)",
                                  (key, now.isoformat(timespec="seconds"), uid, key)).rowcount == 1
        if not claimed:
            continue
        try:
            # Zusammenstellen im try: ein Fehler bei einem Konto darf die übrigen nicht aufhalten
            with connect() as con:
                msg = compose(con, uid, now)
            if msg is None:
                raise RuntimeError(T["err_no_email"])
            send(*msg)
        except Exception as e:
            log.warning("Wochenübersicht für Konto %s nicht gesendet: %s", uid, e)
            with connect() as con:   # Woche wieder freigeben – nächster Versuch frühestens nach RETRY_MINUTES
                con.execute("UPDATE digest_prefs SET last_week=?, last_error=? WHERE user_id=? AND last_week=?",
                            (prev["last_week"] if prev else None, f"{e.__class__.__name__}: {e}"[:300], uid, key))
            continue
        with connect() as con:
            con.execute("UPDATE digest_prefs SET last_sent=?, last_error=NULL WHERE user_id=?",
                        (now.isoformat(timespec="seconds"), uid))
        sent += 1
    return sent


def loop() -> None:
    if config.DEMO:
        return
    while True:
        try:
            run_due()
        except Exception:
            log.exception("Wochenübersicht-Zeitplan")
        time.sleep(LOOP_SECONDS)


def start() -> None:
    threading.Thread(target=loop, name="papigator-digest", daemon=True).start()


# ---------- Routen ----------

def _prefs(con, user_id: int, ui_lang: str | None = None) -> dict:
    """ui_lang: Sprache der Oberfläche, in der die Wochentage genannt werden (sonst die der Mail)."""
    r = con.execute("SELECT * FROM digest_prefs WHERE user_id=?", (user_id,)).fetchone()
    p = dict(r) if r else {"enabled": 0, "weekday": 0, "email": "", "last_sent": None, "last_error": None, "lang": None}
    lang = _lang(con, p.get("lang"))
    return {"enabled": bool(p["enabled"]), "weekday": p["weekday"], "email": p["email"] or "",
            "last_sent": p["last_sent"], "last_error": p["last_error"], "hour": SEND_HOUR,
            "smtp_ready": smtp_settings()["configured"], "links": bool(public_base(con)),
            "lang": lang, "weekdays": weekdays(ui_lang if ui_lang in LANGS else lang)}


class DigestIn(BaseModel):
    enabled: Optional[bool] = None
    weekday: Optional[int] = None
    email: Optional[str] = None
    lang: Optional[str] = None    # Sprache der Mail: de | en; "" = wie der Server


@router.get("/digest")
def digest_get(lang: Optional[str] = None, user: User = Depends(current_user)):
    """Meine Wochenübersicht: an/aus, Wochentag (0 = Montag), Adresse, letzter Versand; smtp_ready = Server kann senden.
    lang = Sprache der Oberfläche für die Namen der Wochentage."""
    with connect() as con:
        return _prefs(con, user.id, lang)


@router.put("/digest")
def digest_put(body: DigestIn, user: User = Depends(current_user)):
    if user.id == 0:
        raise HTTPException(400, T["err_bootstrap"])
    with connect() as con:
        cur = _prefs(con, user.id)
        enabled = cur["enabled"] if body.enabled is None else body.enabled
        weekday = cur["weekday"] if body.weekday is None else body.weekday
        email = cur["email"] if body.email is None else body.email.strip()
        if not 0 <= weekday <= 6:
            raise HTTPException(400, T["err_weekday"])
        if email and not valid_email(email):
            raise HTTPException(400, T["err_email"])
        if enabled and not email:
            raise HTTPException(400, T["err_no_email"])
        row = con.execute("SELECT lang FROM digest_prefs WHERE user_id=?", (user.id,)).fetchone()
        lang = row["lang"] if row else None
        if body.lang is not None:
            lang = body.lang if body.lang in LANGS else None
        con.execute("INSERT INTO digest_prefs(user_id, enabled, weekday, email, lang) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled, weekday=excluded.weekday, "
                    "email=excluded.email, lang=excluded.lang", (user.id, int(enabled), weekday, email, lang))
        return _prefs(con, user.id, body.lang)


_last_test: dict[int, float] = {}
TEST_GAP = 60   # Sekunden zwischen zwei Test-Mails desselben Kontos


@router.post("/digest/test")
def digest_test(user: User = Depends(current_user)):
    """Die Wochenübersicht jetzt an meine Adresse schicken – zählt nicht als Wochenversand."""
    if user.id == 0:
        raise HTTPException(400, T["err_bootstrap"])
    if not smtp_settings()["configured"]:
        raise HTTPException(400, T["err_no_smtp"])
    if time.monotonic() - _last_test.get(user.id, -TEST_GAP) < TEST_GAP:
        raise HTTPException(429, T["err_wait"])
    with connect() as con:
        msg = compose(con, user.id, dt.datetime.now(), test=True)
    if msg is None:
        raise HTTPException(400, T["err_no_email"])
    _last_test[user.id] = time.monotonic()
    try:
        send(*msg)
    except Exception as e:
        raise HTTPException(502, f"Senden fehlgeschlagen: {e.__class__.__name__}: {e}")
    return {"ok": True, "to": msg[0]}


class SmtpIn(BaseModel):
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_security: Optional[str] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None   # leer lassen = unverändert
    smtp_from: Optional[str] = None
    clear_password: Optional[bool] = None


def _smtp_out() -> dict:
    s = smtp_settings()
    s["suggestion"] = suggestion()
    return s


@router.get("/settings/smtp")
def smtp_get(admin: User = Depends(require_admin)):
    """Mail-Versand (für die Wochenübersicht): Einstellungen ohne Passwort, dazu ein Vorschlag aus dem E-Mail-Eingang."""
    return _smtp_out()


@router.patch("/settings/smtp")
def smtp_patch(body: SmtpIn, admin: User = Depends(require_admin)):
    data = body.model_dump(exclude_unset=True)
    if data.get("smtp_security") not in (None, *SECURITY):
        raise HTTPException(400, "Verschlüsselung: starttls, ssl oder none")
    if data.get("smtp_from") and not valid_email(data["smtp_from"].strip()):
        raise HTTPException(400, T["err_email"])
    if data.get("smtp_port") is not None and not 1 <= data["smtp_port"] <= 65535:
        raise HTTPException(400, "Ungültiger Port")
    for k in ("smtp_host", "smtp_user"):
        if data.get(k) and any(c in data[k] for c in "\r\n"):
            raise HTTPException(400, "Ungültige Angabe")
    with connect() as con:
        if data.pop("clear_password", None):
            set_setting(con, "smtp_password", "")
        for k, v in data.items():
            if k == "smtp_password":
                if v:   # leer heißt: so lassen; gespeichert wird nur verschlüsselt
                    set_setting(con, k, security.encrypt(v))
                continue
            set_setting(con, k, "" if v is None else str(v).strip())
    return _smtp_out()


@router.post("/settings/smtp/test")
def smtp_test_route(body: SmtpIn, admin: User = Depends(require_admin)):
    """Verbindung und Anmeldung mit den (ggf. noch ungespeicherten) Werten prüfen – sendet nichts."""
    return smtp_test(body.model_dump(exclude_unset=True, exclude={"clear_password"}))
