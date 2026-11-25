"""Willkommen und „Was ist neu“ (25.11.2026): animierte Karten in Web und App.

Beim ersten Start zeigt der Client eine kurze Tour, nach einem Update die Neuerungen – einmal pro Konto, nicht pro Gerät:
`whatsnew_seen.seen` ist die zuletzt gesehene Ausgabe (RELEASES[i]["id"]). Kein Eintrag = noch nie etwas gesehen → Tour;
Konten, die es beim Einführen schon gab, bekommen „0“ (kennen die App, die Neuerungen aber noch nicht).

Inhalte stehen nur hier. Für einen neuen Stand oben in RELEASES eine Ausgabe ergänzen (id: Datum, aufsteigend sortierbar).
`anim` wählt die Animation im Client (unbekannte zeigen ein schlichtes Bild), `only` beschränkt eine Karte auf "ios" oder
"web". In der Demo gibt es nur die Tour (Besucher sind jeden Tag neu).
"""
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
    mode = show if show in ("tour", "news") else ("tour" if seen is None else ("news" if seen < LATEST else None))
    if config.DEMO and mode == "news" and show != "news":
        mode = None
    if mode == "tour":
        return {"mode": "tour", "cards": _cards(TOUR, client, lang), "release": LATEST, "label": None}
    if mode == "news":
        # auf Wunsch nur die neueste Ausgabe, sonst alles seit der zuletzt gesehenen
        rel = RELEASES[:1] if show == "news" else ([r for r in RELEASES if r["id"] > (seen or "")] or RELEASES[:1])
        cards = [c for r in rel for c in _cards(r["cards"], client, lang)][:6]
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
