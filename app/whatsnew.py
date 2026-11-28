"""Willkommen und „Was ist neu“ (25.11.2026): animierte Karten in Web und App.

Beim ersten Start zeigt der Client eine kurze Tour, nach einem Update die Neuerungen – einmal pro Konto, nicht pro Gerät:
`whatsnew_seen.seen` ist die zuletzt gesehene Ausgabe (RELEASES[i]["id"]). Kein Eintrag = noch nie etwas gesehen → Tour;
Konten, die es beim Einführen schon gab, bekommen „0“ (kennen die App, die Neuerungen aber noch nicht).

Inhalte stehen nur hier. Für einen neuen Stand oben in RELEASES eine Ausgabe ergänzen (id: Datum, aufsteigend sortierbar).
`anim` wählt die Animation im Client (unbekannte zeigen ein schlichtes Bild), `only` beschränkt eine Karte auf "ios" oder
"web". In der Demo gibt es nur die Tour (Besucher sind jeden Tag neu).
"""
import re
from typing import Optional

from fastapi import APIRouter, Depends

from . import config
from .auth import User, current_user
from .db import connect

router = APIRouter()

TOUR = [
    {"anim": "scan", "de": ("Füttere ihn", "Fotografiere Briefe mit der App oder lade PDFs hoch. Papigator liest sie, "
                                           "benennt sie und legt sie richtig ab."),
     "en": ("Feed him", "Snap letters with the app or upload PDFs. Papigator reads, names and files them for you.")},
    {"anim": "search", "de": ("Alles wiederfinden", "Suche in jedem Wort – oder frag einfach: „Wann läuft die "
                                                    "Kfz-Versicherung aus?“"),
     "en": ("Find anything", "Search every word – or just ask: “When does the car insurance run out?”")},
    {"anim": "due", "de": ("Keine Frist verpassen", "Zahlungsziele, Kündigungsfristen und Garantien landen von selbst "
                                                    "unter „Heute“."),
     "en": ("Never miss a deadline", "Due dates, notice periods and warranties show up under “Today” on their own.")},
    {"anim": "money", "de": ("Geld im Blick", "Kontoumsätze aus der Bank-CSV, Verträge, Abos und jährliche Kosten – "
                                              "mit Summen."),
     "en": ("Money at a glance", "Bank statements from CSV, contracts, subscriptions and yearly costs – with totals.")},
    {"anim": "family", "de": ("Gemeinsam ablegen", "Für die ganze Familie – jeder sieht, was er sehen soll. Auch Kinder "
                                                   "ohne eigenes Konto."),
     "en": ("File together", "For the whole family – everyone sees what they should. Even kids without an account.")},
]

# Neueste zuerst
RELEASES = [
    {"id": "2026-11-28", "label": {"de": "November 2026", "en": "November 2026"}, "cards": [
        {"anim": "due", "de": ("Heute nach deinem Geschmack", "Neue Karten auf „Heute“: Kündigen möglich, Steuerjahr, vor einem "
                               "Jahr, zuletzt angesehen und deine Woche. Was du nicht brauchst, blendest du unter „Heute "
                               "anpassen“ aus."),
         "en": ("Today, your way", "New cards on “Today”: time to cancel, tax year, a year ago, recently viewed and your week. "
                "Hide what you don't need under “Customize Today”.")},
        {"anim": "search", "only": "web", "de": ("Ziehen und ablegen", "In der Ordner-Ansicht ziehst du ein Dokument einfach "
                                                 "auf einen anderen Ordner."),
         "en": ("Drag and drop", "In the folder view, simply drag a document onto another folder.")},
    ]},
    {"id": "2026-11-27", "label": {"de": "November 2026", "en": "November 2026"}, "cards": [
        {"anim": "kinds", "de": ("Ordentlich auch ohne KI", "Ohne KI übernimmt Papigator Datum und Absender, wenn sie eindeutig "
                              "im Brief stehen, und Tags, die frühere Briefe desselben Absenders hatten."),
         "en": ("Tidy even without AI", "Without AI, Papigator takes the date and sender when the letter states them "
                "clearly, and the tags earlier letters from the same sender had.")},
        {"anim": "calendar", "only": "web", "de": ("Fristen in Home Assistant", "Das Kalender-Abo gibt es jetzt auch als Sensor: offene "
                                    "und überfällige Fristen auf deinem Dashboard."),
         "en": ("Deadlines in Home Assistant", "The calendar subscription now also works as a sensor: open and overdue "
                "deadlines on your dashboard.")},
    ]},
    {"id": "2026-11-26", "label": {"de": "November 2026", "en": "November 2026"}, "cards": [
        {"anim": "search", "only": "web", "de": ("Ein Feld für alles", "Die Web-App ist neu gebaut: Suchen und Fragen in "
                                                 "einem Feld, am Handy mit Leiste unten wie in der App."),
         "en": ("One field for everything", "The web app has been rebuilt: search and ask in one field, with a tab bar "
                "on phones just like the app.")},
        {"anim": "pay", "de": ("Bezahlen per QR", "Rechnungen mit IBAN zeigen einen GiroCode für deine Banking-App. "
                               "Einschalten in den Einstellungen – und Betrag mit dem Brief vergleichen."),
         "en": ("Pay by QR code", "Invoices with an IBAN show a GiroCode for your banking app. Turn it on in Settings – "
                "and check the amount against the letter.")},
        {"anim": "connect", "only": "ios", "de": ("Server findet sich von selbst", "Im Heimnetz zeigt die Anmeldung deinen "
                                                  "Papigator von allein – oder den QR-Code „App verbinden“ aus dem Web scannen."),
         "en": ("Your server finds itself", "On your home network, sign-in shows your Papigator on its own – or scan the "
                "“Connect app” QR code from the web.")},
        {"anim": "connect", "only": "web", "de": ("App verbinden", "Unter Einstellungen zeigt „App verbinden“ einen QR-Code. "
                                                  "Mit dem iPhone scannen – Adresse und Name sind schon eingetragen."),
         "en": ("Connect the app", "In Settings, “Connect app” shows a QR code. Scan it with your iPhone – address and "
                "name are filled in.")},
        {"anim": "ai", "only": "ios", "de": ("Sehen, was die KI tut", "„Heute“ zeigt, wie viele Dokumente die KI gerade "
                                             "liest und ergänzt."),
         "en": ("See what the AI is doing", "“Today” shows how many documents the AI is reading and filling in.")},
        {"anim": "scan", "only": "web", "de": ("Fotos statt Scanner", "Lade Fotos von Briefen hoch – mehrere Seiten werden "
                                               "ein PDF."),
         "en": ("Photos instead of a scanner", "Upload photos of letters – several pages become one PDF.")},
        {"anim": "import", "only": "web", "de": ("Umzug von Paperless", "Bring deinen Paperless-Export mit: Titel, Tags, "
                                                 "Absender und Text kommen mit (Einstellungen, nur Admins)."),
         "en": ("Moving from Paperless", "Bring your Paperless export: titles, tags, correspondents and text come along "
                "(Settings, admins only).")},
        {"anim": "warranty", "de": ("Checkliste für die Notfallmappe", "Die Notfallmappe zeigt, was hineingehört, was schon "
                                    "drin ist – und schlägt passende Dokumente aus deiner Ablage vor."),
         "en": ("Checklist for the emergency folder", "The emergency folder shows what belongs in it, what is already "
                "there – and suggests matching documents from your files.")},
        {"anim": "lang", "de": ("Papigator spricht Englisch", "App, Web und KI gibt es jetzt auch auf Englisch."),
         "en": ("Papigator speaks English", "The app, the web and the AI now speak English too.")},
        {"anim": "mail", "de": ("Die Woche per Mail", "Neue Dokumente und offene Fristen einmal pro Woche im Postfach – "
                                "ohne Beträge. Einschalten in den Einstellungen."),
         "en": ("Your week by email", "New documents and open deadlines in your inbox once a week – no amounts. Turn it "
                "on in Settings.")},
    ]},
    {"id": "2026-11-25.2", "label": {"de": "November 2026", "en": "November 2026"}, "cards": [
        {"anim": "kinds", "de": ("Jedes Dokument, nicht nur Rechnungen", "Papigator erkennt jetzt auch Art, Empfänger und "
                                 "Nummern – vom Zeugnis bis zum Befund. Filtern kannst du über „Art“."),
         "en": ("Every document, not just invoices", "Papigator now also recognizes the type, recipient and reference "
                "numbers – from school reports to medical reports. Filter by “Type”.")},
    ]},
    {"id": "2026-11-25", "label": {"de": "November 2026", "en": "November 2026"}, "cards": [
        {"anim": "notes", "de": ("Notizen am Dokument", "Schreib dazu, was wichtig ist – alle, die das Dokument sehen, "
                                                        "lesen mit."),
         "en": ("Notes on documents", "Jot down what matters – everyone who sees the document can read it.")},
        {"anim": "calendar", "de": ("Fristen im Kalender", "Abonniere alle offenen Fristen in deinem Kalender – sie "
                                                           "aktualisieren sich von selbst."),
         "en": ("Deadlines in your calendar", "Subscribe to all open deadlines in your calendar – they stay up to date.")},
        {"anim": "cancel", "de": ("Kündigungshelfer", "Ein Tipp beim Vertrag, und das Kündigungsschreiben ist fertig – "
                                                      "als PDF oder gleich abgelegt."),
         "en": ("Cancellation helper", "One tap on a contract and the cancellation letter is ready – as a PDF or filed.")},
        {"anim": "warranty", "de": ("Garantie im Blick", "Läuft eine Garantie in vier Wochen ab, erinnert „Im Blick“ "
                                                         "daran."),
         "en": ("Warranty on watch", "When a warranty ends in four weeks, “Coming up” reminds you.")},
        {"anim": "avatars", "de": ("Gesichter statt Buchstaben", "Profilbilder für dich und alle in der Familie – auch "
                                                                 "für Kinder ohne Konto."),
         "en": ("Faces, not letters", "Profile pictures for you and the whole family – kids without an account too.")},
        {"anim": "appicon", "only": "ios", "de": ("Dein App-Icon", "Unter Einstellungen → Darstellung: Alligator, "
                                                                   "Buchstabe, Stapel oder Ordner."),
         "en": ("Your app icon", "In Settings → Appearance: alligator, letter, stack or folder.")},
    ]},
]
LATEST = RELEASES[0]["id"]


def _cards(items, client: str, lang: str) -> list[dict]:
    out = []
    for c in items:
        if c.get("only") and c["only"] != client:
            continue
        title, text = c.get(lang) or c["de"]
        out.append({"anim": c["anim"], "title": title, "text": text})
    return out


def _key(rid: Optional[str]) -> tuple:
    """„2026-11-25.10“ → (2026, 11, 25, 10): Zahlen vergleichen, nicht Text (sonst wäre .10 älter als .2)."""
    return tuple(int(n) for n in re.findall(r"\d+", rid or ""))


def _seen(con, uid: int) -> Optional[str]:
    r = con.execute("SELECT seen FROM whatsnew_seen WHERE user_id=?", (uid,)).fetchone()
    return r["seen"] if r else None


@router.get("/whatsnew")
def whatsnew(client: str = "web", lang: str = "de", show: Optional[str] = None, user: User = Depends(current_user)):
    """Was der Client jetzt zeigen soll: mode "tour" | "news" | null (nichts). show=tour|news zeigt es auf Wunsch
    (Einstellungen → „Was ist neu“), auch wenn es schon gesehen wurde."""
    client = "ios" if client == "ios" else "web"
    lang = "en" if lang.lower().startswith("en") else "de"
    if user.id == 0:
        return {"mode": None, "cards": [], "release": LATEST, "label": None}
    with connect() as con:
        seen = _seen(con, user.id)
    mode = show if show in ("tour", "news") else ("tour" if seen is None else ("news" if _key(seen) < _key(LATEST) else None))
    if config.DEMO and mode == "news" and show != "news":
        mode = None
    if mode == "tour":
        return {"mode": "tour", "cards": _cards(TOUR, client, lang), "release": LATEST, "label": None}
    if mode == "news":
        # auf Wunsch nur die neueste Ausgabe, sonst alles seit der zuletzt gesehenen
        rel = RELEASES[:1] if show == "news" else ([r for r in RELEASES if _key(r["id"]) > _key(seen)] or RELEASES[:1])
        cards = [c for r in rel for c in _cards(r["cards"], client, lang)][:8]
        return {"mode": "news", "cards": cards, "release": LATEST, "label": rel[0]["label"][lang]}
    return {"mode": None, "cards": [], "release": LATEST, "label": None}


@router.post("/whatsnew/seen", status_code=204)
def whatsnew_seen(user: User = Depends(current_user)):
    """Tour oder Neuerungen gesehen (auch „Überspringen“) – kommt bis zur nächsten Ausgabe nicht wieder."""
    if user.id == 0:
        return
    with connect() as con:
        con.execute("INSERT INTO whatsnew_seen(user_id, seen) VALUES (?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET seen=excluded.seen", (user.id, LATEST))
