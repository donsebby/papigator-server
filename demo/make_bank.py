"""Demo-Kontoumsätze: erfundener Haushalt Alex & Sam Muster, ein gutes Jahr bis heute, im Format eines
C24-Exports (alle Konten in einer Datei, Buchungsart vorne, Kategorie der Bank hinten).

Zwei Exporte wie im echten Leben:
- Alex: Gemeinschaftskonto, Pockets „Urlaub“ und „Jährlich“ (beide sehen sie) und das eigene Sparkonto (privat)
- Sam: dasselbe Gemeinschaftskonto der letzten Monate (läuft ohne Doppelte zusammen) und das eigene Sparkonto (privat)

Buchungen zu Belegen im Demo-Archiv (Funkwerk, Marktplatz24, Werkstatt, HanseKraft …) tragen Betrag und Datum des Belegs, damit
Papigator sie verknüpft. Ein paar Händler kennt Papigator absichtlich nicht – die landen unter „Einsortieren“.
Alle Namen, Nummern und Beträge sind erfunden.

    from make_bank import exports
    files = exports(date.today(), docs)   # docs: [{"file", "date" (JJJJ-MM-TT), "amount"}] – schon verschoben
"""
from __future__ import annotations

import calendar
import random
from datetime import date, timedelta

HEAD = ("Transaktionstyp;Buchungsdatum;Wertstellung;Betrag;Zahlungsempfänger;IBAN;BIC;Verwendungszweck;"
        "Kontoinhaber;Kontonummer;Kontoname;Kategorie;Unterkategorie")
GIRO = ("1000000001", "Gemeinschaftskonto")
URLAUB = ("1000000002", "Urlaub")
JAEHRLICH = ("1000000005", "Jährlich")   # Rücklagen-Topf für Versicherungen, Jahresabos, Beiträge
SPAR_A = ("3000000003", "Sparkonto Alex")
SPAR_S = ("4000000004", "Sparkonto Sam")
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November",
          "Dezember"]


def _eur(cents: int) -> str:
    s = f"{abs(cents) / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'-' if cents < 0 else ''}{s} €"


def _day(y: int, m: int, d: int) -> date:
    return date(y, m, min(d, calendar.monthrange(y, m)[1]))


def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def exports(today: date, docs: list[dict]) -> dict[str, bytes]:
    from shift import BASE
    rnd = random.Random(11)
    shift = today - BASE   # feste Termine der Demo-Texte wandern mit
    start = _day(today.year - 2, today.month, 1) - timedelta(days=31)   # gut zwei Jahre (Jährliche Kosten: Vorjahr)
    rows: list[tuple] = []   # (Konto, Datum, Cent, Art, Gegenüber, Zweck, Bankkategorie)

    def add(acc, when: date, cents: int, kind: str, who: str, purpose: str, cat: str = ""):
        if start <= when <= today:
            rows.append((acc, when, cents, kind, who, purpose, cat))

    def pocket(src, dst, when: date, cents: int, purpose: str):
        add(src, when, -cents, "Pocket-Umbuchung", dst[1], purpose, "Umbuchung / Umbuchung")
        add(dst, when, cents, "Pocket-Umbuchung", src[1], purpose, "Umbuchung / Umbuchung")

    # --- Monatliches
    for y, m in _months(start, today):
        mon = f"{MONATE[m - 1]} {y}"
        add(GIRO, _day(y, m, 1), -115000, "Dauerauftrag", "Hausverwaltung Kern & Partner", "Miete Lindenweg 12, 2. OG links")
        add(GIRO, _day(y, m, 3), -18600, "Lastschrift", "Stadt Karlsruhe", f"Kita-Gebühren Mia {mon}")
        add(GIRO, _day(y, m, 10), -7200, "Lastschrift", "Stadtwerke Lindenau", f"Abschlag Strom/Gas {mon}")
        add(GIRO, _day(y, m, 6), -1399, "Lastschrift", "Filmriss Streaming B.V.", "Filmriss Standard", "Abos / Streaming")
        # Preiserhöhung vor gut drei Monaten – zeigt „Aus dem Konto erkannt“
        spot = 1799 if _day(y, m, 12) >= today - timedelta(days=100) else 1699
        add(GIRO, _day(y, m, 12), -spot, "Lastschrift", "Klangwerk Music AB", "Klangwerk Familie", "Abos / Musik")
        add(GIRO, _day(y, m, 8), -1427, "Lastschrift", "Pixelwerk Software Ltd", "Foto-Abo Plus", "Abos / Software")
        add(GIRO, _day(y, m, 18), 25500, "Gutschrift", "Familienkasse Baden-Württemberg", f"Kindergeld {mon}")
        add(GIRO, _day(y, m, 28), 145000, "Gutschrift", "Kanzlei Weber & Roth", f"Gehalt {mon}", "Einkommen / Gehalt")
        add(GIRO, _day(y, m, 20), -10000, "Auszahlung", "Geldautomat Lindenbank", "Bargeldauszahlung")
        pocket(GIRO, URLAUB, _day(y, m, 2), 15000, "Urlaubskasse")
        pocket(GIRO, SPAR_A, _day(y, m, 2), 20000, "Sparplan Alex")
        pocket(GIRO, SPAR_S, _day(y, m, 2), 10000, "Sparen Sam")
        pocket(GIRO, JAEHRLICH, _day(y, m, 2), 8500, "Rücklage Jährliches")
        if _day(y, m, 15) < date(2025, 12, 31) + shift:   # Fitnessstudio bis zur Kündigung (wandert mit)
            add(GIRO, _day(y, m, 15), -2499, "Lastschrift", "FitPunkt Studios GmbH", "Mitgliedsbeitrag", "Freizeit / Sport")
        if m % 3 == 1:
            add(GIRO, _day(y, m, 15), -5508, "Lastschrift", "Rundfunk ARD ZDF DRadio", "Rundfunkbeitrag Quartal")
            add(GIRO, _day(y, m, 9), -4500, "Lastschrift", "Musikschule Fächerstadt e.V.", "Gitarrenunterricht Quartal")
        # Alltag: Wocheneinkauf, Drogerie, Tanken, mal essen gehen
        for wk in range(4):
            d0 = _day(y, m, 2 + wk * 7 + rnd.randint(0, 2))
            shop = rnd.choice(["Frischmarkt Lindenau", "Sparkauf Süd", "Kaufpark Center Karlsruhe", "Frischmarkt Lindenau"])
            add(GIRO, d0, -rnd.randint(3800, 13500), "Kartenzahlung", shop, "Kartenzahlung Girokarte", "Lebensmittel / Supermarkt")
            add(GIRO, d0 + timedelta(days=3), -rnd.randint(450, 1400), "Kartenzahlung", "Bäckerei Nussbaum", "Kartenzahlung")
        add(GIRO, _day(y, m, 11), -rnd.randint(1800, 4600), "Kartenzahlung", "Drogerie Blüte", "Kartenzahlung", "Haushalt / Drogerie")
        for d in (7, 22):
            add(GIRO, _day(y, m, d), -rnd.randint(5800, 8400), "Kartenzahlung", "Tankstelle Durlacher Allee", "Tanken", "Mobilität / Tanken")
        if rnd.random() < 0.7:
            add(GIRO, _day(y, m, 16), -rnd.randint(2400, 5200), "Kartenzahlung", "Pizzabote Lindenau", "Bestellung", "Freizeit / Essen")
        if rnd.random() < 0.5:
            add(GIRO, _day(y, m, 24), -rnd.randint(3500, 9800), "Kartenzahlung", rnd.choice(["Modehaus Krone", "Kleiderkiste", "Schuhwerk Lindenau"]),
                "Kartenzahlung", "Kleidung / Bekleidung")

    # Gehalt Alex: wie die Entgeltabrechnungen im Archiv
    for x in docs:
        if x["file"].startswith("Entgeltabrechnung_") and x.get("amount") and x.get("date"):
            when = date.fromisoformat(x["date"])
            add(GIRO, when, int(round(x["amount"] * 100)), "Gutschrift", "Kranich Logistik GmbH",
                f"Gehalt {MONATE[when.month - 1]} {when.year}", "Einkommen / Gehalt")

    # --- Buchungen zu Belegen: Betrag und Datum wie im Dokument, damit Papigator sie verknüpft
    pay = {   # Dateianfang → (Tage nach Belegdatum, Art, Gegenüber, Zweck, Bankkategorie)
        "Funkwerk_Rechnung_": (10, "Lastschrift", "Funkwerk Mobil GmbH", "Mobilfunk Kd-Nr 4711 0815 2", "Wohnen / Telefon"),
        "Marktplatz24_20": (1, "Lastschrift", "MARKTPLATZ 24 GMBH", "Bestellung 302-4471190", "Online-Shopping / Versand"),
        "Werkstatt_": (3, "Überweisung", "Autohaus Süd GmbH", "Rechnung Werkstatt", "Mobilität / Werkstatt"),
        "HanseKraft_Beitragsrechnung_": (13, "Lastschrift", "HANSE KRAFT VERSICHERUNG", "Kfz-Versicherung Beitrag", ""),
        "Haftpflicht_": (5, "Lastschrift", "Lindwurm Versicherung AG", "Privathaftpflicht Beitrag", ""),
        "Zahnarzt_": (8, "Überweisung", "Zahnarztpraxis Dr. Weiß", "Rechnung Behandlung", ""),
        "Mahnung_Sanitaer": (4, "Überweisung", "Sanitär Becker Meisterbetrieb", "Rechnung inkl. Mahngebühr", "Wohnen / Handwerker"),
        "Heizungswartung_": (6, "Überweisung", "Sanitär Becker Meisterbetrieb", "Wartung Gastherme", "Wohnen / Handwerker"),
        "Kaufbeleg_Fernseher": (0, "Kartenzahlung", "Elektro Lindner GmbH", "Kartenzahlung", "Haushalt / Elektronik"),
        "Arztrechnung_": (9, "Überweisung", "Praxis Dr. Hofmann", "Rechnung nach GOÄ", ""),
        "Stadtwerke_Jahresabrechnung_": (14, "Lastschrift", "Stadtwerke Lindenau", "Nachzahlung Jahresabrechnung", ""),
        "Nebenkosten_2025": (12, "Überweisung", "Hausverwaltung Kern & Partner", "Nachzahlung Nebenkosten",
                             "Wohnen / Nebenkosten"),
        "Spende_": (0, "Überweisung", "Weltweit Helfen e.V.", "Spende", "Sonstiges / Spenden"),
    }
    for x in docs:
        hit = next((v for k, v in pay.items() if x["file"].startswith(k)), None)
        if not hit or not x.get("amount") or not x.get("date"):
            continue
        days, kind, who, purpose, cat = hit
        add(GIRO, date.fromisoformat(x["date"]) + timedelta(days=days), -int(round(x["amount"] * 100)), kind, who, purpose, cat)
        if x["file"] == "Marktplatz24_Gutschrift.pdf":
            rows.pop()
            add(GIRO, date.fromisoformat(x["date"]) + timedelta(days=1), int(round(x["amount"] * 100)), "Gutschrift",
                "MARKTPLATZ 24 GMBH", "Erstattung Rücksendung", "Online-Shopping / Versand")

    # --- Jährliches: vom Giro abgebucht und aus dem Topf „Jährlich“ ausgeglichen; die Domain läuft über ein Konto,
    # das Papigator nicht kennt – dafür gibt es nur die Entnahme aus dem Topf
    yearly = [   # (Monat, Tag, Gegenüber, Zweck, Cent je Jahr ab vorletztem Jahr, Bankkategorie)
        (1, 14, "Autoclub Süd e.V.", "Mitgliedsbeitrag Plus", (8900, 9400, 9400), "Mobilität / Verein"),
        (6, 3, "Bundeskasse Kfz-Steuer", "Kfz-Steuer KA-AM 512", (12000, 12000, 12000), ""),
        (9, 21, "Pixelplay Network Ltd", "Pixelplay Plus Jahresabo", (5999, 6999, 6999), "Abos / Spiele"),
    ]
    for mon, d, who, purpose, prices, cat in yearly:
        for k, cents in enumerate(prices):
            when = _day(today.year - 2 + k, mon, d) + shift
            add(GIRO, when, -cents, "Lastschrift", who, purpose, cat)
            pocket(JAEHRLICH, GIRO, when - timedelta(days=1), cents, purpose.split(" ")[0])
    for k, cents in enumerate((3600, 3600, 3900)):
        pocket(JAEHRLICH, GIRO, _day(today.year - 2 + k, 4, 8) + shift, cents, "Domain und Webspace")
    for x in docs:   # Kfz- und Haftpflicht-Beitrag (Belege im Archiv) ebenfalls aus dem Topf
        if x.get("amount") and x.get("date") and x["file"].startswith(("HanseKraft_Beitragsrechnung_", "Haftpflicht_")):
            pocket(JAEHRLICH, GIRO, date.fromisoformat(x["date"]) + timedelta(days=10), int(round(x["amount"] * 100)),
                   "Kfz-Versicherung" if x["file"].startswith("Hanse") else "Haftpflicht")

    # --- Urlaub im Sommer: aus der Urlaubskasse bezahlt
    summer = date(today.year if today.month > 8 else today.year - 1, 8, 4)
    pocket(URLAUB, GIRO, summer - timedelta(days=3), 120000, "Sommerurlaub")
    add(GIRO, summer, -118400, "Überweisung", "Ferienhaus Ostseeblick", "Ferienwohnung 2 Wochen", "Freizeit / Reisen")
    # Zinsen auf den Sparkonten
    for acc in (SPAR_A, SPAR_S):
        add(acc, _day(today.year, 1, 2) if today.month > 1 else _day(today.year - 1, 7, 1), 1840, "Gutschrift",
            "Lindenbank AG", "Zinsen", "Einkommen / Zinsen")

    # --- Kennt Papigator nicht: zum Einsortieren (einmal zugeordnet, merkt es sich den Händler)
    for back in (9, 23, 64):   # unregelmäßig – kein „neues Abo“
        add(GIRO, today - timedelta(days=back), -rnd.randint(1400, 3200), "Kartenzahlung", "Hofladen Kühn", "Kartenzahlung")
    for back, what in ((21, "Flohmarkt online Kinderwagen"), (48, "Flohmarkt online Fahrradanhänger")):
        add(GIRO, today - timedelta(days=back), -rnd.randint(4500, 9000), "Lastschrift", "Bezahldienst Europa",
            f"{what}")

    rows.sort(key=lambda r: (r[1], r[0][0]), reverse=True)

    def csv(accounts, since: date | None = None) -> bytes:
        out = [HEAD]
        for acc, when, cents, kind, who, purpose, cat in rows:
            if acc not in accounts or (since and when < since):
                continue
            main, sub = (cat.split(" / ") + [""])[:2] if cat else ("", "")
            owner = "Sam Muster" if acc == SPAR_S else "Alex Muster"
            out.append(";".join([kind, when.strftime("%d.%m.%Y"), "", _eur(cents), who, "", "", purpose, owner,
                                 acc[0], acc[1], main, sub]))
        return ("\n".join(out) + "\n").encode()

    return {"alex": csv({GIRO, URLAUB, JAEHRLICH, SPAR_A}),
            "sam": csv({GIRO, URLAUB, JAEHRLICH, SPAR_S}, since=today - timedelta(days=75))}
