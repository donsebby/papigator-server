"""Demo-Daten: erfundener Haushalt Alex & Sam Muster, realistische Dokumenttexte 2024–2026 (wie nach OCR) + PDFs.

Für Tests der Geld-Funktionen und als Grundstock eines Demoservers (App-Prüfung). Alle Namen, Adressen,
Nummern und IBANs sind erfunden. Schreibt docs.json (owner, file, text, date, truth = was erkannt werden sollte)
und die PDFs nach <Ausgabe>/pdf/.

    pip install fpdf2
    python make_docs.py /tmp/papigator-demo
"""
import json
import os
import random
import sys
from datetime import date, timedelta
from pathlib import Path

from fpdf import FPDF

random.seed(7)
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "papigator-demo")
(OUT / "pdf").mkdir(parents=True, exist_ok=True)
FONT = next((f for f in [os.environ.get("DEMO_FONT", ""), "/System/Library/Fonts/Supplemental/Arial.ttf",
                         "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"] if f and Path(f).exists()), None)
ADR_A = "Alex Muster\nLindenweg 12\n76137 Karlsruhe"
ADR_S = "Sam Muster\nLindenweg 12\n76137 Karlsruhe"
docs = []


def eur(v):
    s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} €"


def d(s):
    return date.fromisoformat(s)


def de(x: date):
    return x.strftime("%d.%m.%Y")


def add(owner, when: date, fname, text, **truth):
    docs.append({"owner": owner, "file": fname, "text": text.strip(), "date": when.isoformat(), "truth": truth})


MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]

# --- Mobilfunk Funkwerk: monatlich, leicht schwankend (Roaming), 2025-01 .. 2026-10
for i in range(22):
    m = date(2025, 1, 1) + timedelta(days=31 * i)
    m = date(m.year, m.month, 4)
    amt = 39.95 + (random.choice([0, 0, 0, 0, 4.99, 2.40]))
    if m >= date(2026, 4, 1):
        amt += 3.00   # Preiserhöhung
    rn = f"{random.randint(100000000, 999999999)}"
    add("alex", m, f"Funkwerk_Rechnung_{m:%Y_%m}.pdf", f"""
Funkwerk Mobil GmbH · Wellenweg 5 · 53111 Bonn
{ADR_A}
Ihre Rechnung für {MONATE[m.month-1]} {m.year}
Kundennummer 4711 0815 2  Rechnungsnummer {rn}
Rechnungsdatum {de(m)}
FunkMobil M  monatlicher Grundpreis  {eur(39.95 + (3 if m >= date(2026,4,1) else 0))}
Verbindungen / Roaming                     {eur(amt - 39.95 - (3 if m >= date(2026,4,1) else 0))}
Rechnungsbetrag (inkl. 19 % USt)           {eur(amt)}
Der Rechnungsbetrag wird am {de(m + timedelta(days=10))} von Ihrem Konto DE12 3456 7890 1234 5678 90 abgebucht.
""", kind="Rechnung", amount=round(amt, 2), direction="Ausgabe")

# Preiserhöhung angekündigt
add("alex", d("2026-02-20"), "Funkwerk_Preisanpassung.pdf", f"""
Funkwerk Mobil GmbH · 53111 Bonn
{ADR_A}
{de(d('2026-02-20'))}
Information zu Ihrem Vertrag FunkMobil M – Preisanpassung
Sehr geehrter Herr Muster, ab dem 01.04.2026 erhöht sich der monatliche Grundpreis von 39,95 € auf 42,95 €.
Sie haben ein Sonderkündigungsrecht bis zum 31.03.2026.
""", kind="Vertragsänderung", amount=None, due="2026-03-31")

# --- Gehalt Alex monatlich
for i in range(22):
    m = date(2025, 1, 25) + timedelta(days=31 * i)
    m = date(m.year, m.month, 25)
    brutto = 4650.00 if m < date(2026, 1, 1) else 4790.00
    netto = round(brutto * 0.6118, 2)
    add("alex", m, f"Entgeltabrechnung_{m:%Y_%m}.pdf", f"""
Kranich Logistik GmbH · Personalabteilung · Hafenstraße 3 · 76189 Karlsruhe
{ADR_A}
Abrechnung der Brutto/Netto-Bezüge für {MONATE[m.month-1]} {m.year}
Personalnummer 20417  Steuerklasse IV  Kirchensteuer –
Gehalt                      {eur(brutto)}
Gesamtbrutto                {eur(brutto)}
Lohnsteuer                  {eur(round(brutto*0.142,2))}
Solidaritätszuschlag        0,00 €
KV/RV/AV/PV-Beitrag AN      {eur(round(brutto*0.2464,2))}
Auszahlungsbetrag           {eur(netto)}
Überweisung auf DE12 3456 7890 1234 5678 90
""", kind="Gehalt", amount=netto, direction="Einnahme")

# --- Kfz-Versicherung jährlich
for y, amt in [(2024, 612.40), (2025, 655.80), (2026, 701.20)]:
    dd = date(y - 1, 11, 18)
    add("alex", dd, f"HanseKraft_Beitragsrechnung_{y}.pdf", f"""
Hanse Kraft Versicherung AG · 20095 Hamburg
{ADR_A}
Hamburg, {de(dd)}
Beitragsrechnung zur Kraftfahrtversicherung Versicherungsschein-Nr. 123/456789-K
Fahrzeug: Avero K4 Kombi, KA-AM 512
Versicherungszeitraum 01.01.{y} – 31.12.{y}
Haftpflicht SF 12  {eur(round(amt*0.55,2))}
Vollkasko SF 9     {eur(round(amt*0.45,2))}
Jahresbeitrag      {eur(amt)}
Fällig am 01.01.{y}. Der Beitrag wird per Lastschrift eingezogen.
Sie können den Vertrag bis zum 30.11.{y-1} zum Ablauf kündigen.
""", kind="Versicherung", amount=amt, direction="Ausgabe", due=f"{y-1}-11-30")

# --- Haftpflicht jährlich (Sam)
for y, amt in [(2024, 68.90), (2025, 68.90), (2026, 74.10)]:
    dd = date(y, 2, 2)
    add("sam", dd, f"Haftpflicht_{y}.pdf", f"""
Lindwurm Versicherung AG · 10115 Berlin
{ADR_S}
{de(dd)}
Beitragsrechnung Privathaftpflicht Familie – Vertrag AS-998877
Zeitraum 01.03.{y} – 28.02.{y+1}
Jahresbeitrag {eur(amt)} inkl. 19 % Versicherungsteuer
Abbuchung zum 01.03.{y}
""", kind="Versicherung", amount=amt, direction="Ausgabe")

# --- Rundfunkbeitrag vierteljährlich (Bescheid jährlich, aber Zahlungsaufforderung je Quartal hier nur einmal jährlich)
add("alex", d("2025-01-10"), "Rundfunkbeitrag_2025.pdf", f"""
ARD ZDF Deutschlandradio Beitragsservice · 50656 Köln
{ADR_A}
Beitragsnummer 412 345 678
Zahlungsaufforderung {de(d('2025-01-10'))}
Für den Zeitraum 01.01.2025 – 31.12.2025 sind vierteljährlich 55,08 € zu zahlen (monatlich 18,36 €).
Fälligkeiten: 15.02., 15.05., 15.08., 15.11.2025 – Abbuchung per Lastschrift.
""", kind="Bescheid", amount=55.08, direction="Ausgabe")

# --- Strom Stadtwerke: Jahresabrechnung mit Nachzahlung / Guthaben + Abschlagsplan
add("alex", d("2025-02-14"), "Stadtwerke_Jahresabrechnung_2024.pdf", f"""
Stadtwerke Lindenau GmbH · Am Wasserwerk 3 · 76185 Karlsruhe
{ADR_A}
Jahresabrechnung Strom 2024  Vertragskonto 300 445 112
Abrechnungsdatum {de(d('2025-02-14'))}
Verbrauch 01.01.2024 – 31.12.2024: 3.412 kWh
Kosten gesamt                  1.304,58 €
abzüglich geleistete Abschläge 1.188,00 € (12 × 99,00 €)
Nachzahlung                      116,58 €
Bitte überweisen Sie den Betrag bis zum 07.03.2025.
Neuer monatlicher Abschlag ab März 2025: 109,00 €
""", kind="Abrechnung", amount=116.58, direction="Ausgabe (Nachzahlung)", total_cost=1304.58, due="2025-03-07")
add("alex", d("2026-02-12"), "Stadtwerke_Jahresabrechnung_2025.pdf", f"""
Stadtwerke Lindenau GmbH · Am Wasserwerk 3 · 76185 Karlsruhe
{ADR_A}
Jahresabrechnung Strom 2025  Vertragskonto 300 445 112
Abrechnungsdatum {de(d('2026-02-12'))}
Verbrauch 01.01.2025 – 31.12.2025: 3.105 kWh
Kosten gesamt                  1.211,40 €
abzüglich geleistete Abschläge 1.288,00 €
Guthaben                          76,60 €
Das Guthaben überweisen wir Ihnen in den nächsten Tagen auf Ihr Konto.
Neuer monatlicher Abschlag ab März 2026: 101,00 €
""", kind="Abrechnung", amount=76.60, direction="Einnahme (Guthaben)", total_cost=1211.40)

# --- Nebenkostenabrechnung Vermieter
add("alex", d("2025-06-03"), "Nebenkosten_2024.pdf", f"""
Hausverwaltung Kern & Partner · Kaiserstraße 50 · 76133 Karlsruhe
{ADR_A}
{de(d('2025-06-03'))}
Betriebskostenabrechnung 2024 für die Wohnung Lindenweg 12, 2. OG links
Grundsteuer 312,40 €  Wasser/Abwasser 488,10 €  Müll 201,00 €  Hausmeister 356,00 €  Gartenpflege 144,00 €
Treppenhausreinigung 260,00 €  Gebäudeversicherung 198,70 €  Heizung (Wärmemessdienst) 1.120,30 €
Summe Ihrer Kosten 3.080,50 €
geleistete Vorauszahlungen 3.240,00 €
Guthaben 159,50 €  – wird mit der Miete für Juli verrechnet.
Hinweis: In den Kosten sind haushaltsnahe Dienstleistungen nach § 35a EStG enthalten (Hausmeister, Gartenpflege, Treppenhausreinigung): 760,00 €.
""", kind="Abrechnung", amount=159.50, direction="Einnahme (Guthaben)", tax35a=760.00)
add("alex", d("2026-05-28"), "Nebenkosten_2025.pdf", f"""
Hausverwaltung Kern & Partner · Kaiserstraße 50 · 76133 Karlsruhe
{ADR_A}
{de(d('2026-05-28'))}
Betriebskostenabrechnung 2025 für die Wohnung Lindenweg 12, 2. OG links
Summe Ihrer Kosten 3.402,80 €
geleistete Vorauszahlungen 3.240,00 €
Nachzahlung 162,80 € – bitte bis 30.06.2026 überweisen.
Haushaltsnahe Dienstleistungen nach § 35a EStG: 790,00 €
Ab 01.07.2026 beträgt die monatliche Vorauszahlung 285,00 € (bisher 270,00 €).
""", kind="Abrechnung", amount=162.80, direction="Ausgabe (Nachzahlung)", tax35a=790.00, due="2026-06-30")

# --- Steuerbescheid mit Erstattung
add("alex", d("2025-07-22"), "Steuerbescheid_2024.pdf", f"""
Finanzamt Karlsruhe-Durlach · Prinzessenstraße 2 · 76227 Karlsruhe
Alex und Sam Muster
Lindenweg 12, 76137 Karlsruhe
Steuernummer 34/123/45678  Datum {de(d('2025-07-22'))}
Bescheid für 2024 über Einkommensteuer, Solidaritätszuschlag
Festgesetzt werden: Einkommensteuer 9.874,00 €  Solidaritätszuschlag 0,00 €
abzüglich Steuerabzug vom Lohn 11.212,00 €
Erstattung 1.338,00 €  – Der Betrag wird auf das Konto DE12 3456 7890 1234 5678 90 überwiesen.
Rechtsbehelfsbelehrung: Der Bescheid kann innerhalb eines Monats nach Bekanntgabe mit dem Einspruch angefochten werden.
""", kind="Bescheid", amount=1338.00, direction="Einnahme (Erstattung)", due="2025-08-25")

# --- Handwerker (§35a)
add("alex", d("2025-09-15"), "Rechnung_Sanitaer_Becker.pdf", f"""
Sanitär Becker Meisterbetrieb · Rintheimer Str. 9 · 76131 Karlsruhe
{ADR_A}
Rechnung Nr. 2025-0918  Datum {de(d('2025-09-15'))}
Austausch Spülkasten und Eckventile Bad, Lindenweg 12
Arbeitslohn 3,5 Std. à 68,00 €     238,00 €
Anfahrt                             35,00 €
Material                           187,40 €
Nettobetrag                        460,40 €
zzgl. 19 % USt                      87,48 €
Rechnungsbetrag                    547,88 €
Davon Lohnkosten inkl. USt (§ 35a EStG): 324,87 €
Zahlbar innerhalb 14 Tagen ohne Abzug bis {de(d('2025-09-29'))}.
""", kind="Rechnung", amount=547.88, direction="Ausgabe", tax35a=324.87, due="2025-09-29")

# --- Spenden
for dd, org, amt in [("2025-12-20", "Weltweit Helfen e.V.", 150.00), ("2025-03-02", "Tierfreunde Lindenau e.V.", 60.00), ("2026-01-15", "Weltweit Helfen e.V.", 50.00)]:
    add("alex", d(dd), f"Spende_{dd}.pdf", f"""
{org}
Zuwendungsbestätigung / Spendenquittung
Bestätigung über Geldzuwendungen im Sinne des § 10b EStG
Name des Zuwendenden: Alex Muster, Lindenweg 12, 76137 Karlsruhe
Betrag der Zuwendung: {eur(amt)}  Tag der Zuwendung: {de(d(dd))}
Es handelt sich nicht um den Verzicht auf Erstattung von Aufwendungen.
""", kind="Spende", amount=amt, direction="Ausgabe", tax=True)

# --- Arzt / Apotheke (Sam)
add("sam", d("2025-04-11"), "Zahnarzt_Rechnung.pdf", f"""
Zahnarztpraxis Dr. Weiß · Kriegsstraße 100 · 76133 Karlsruhe
{ADR_S}
Rechnung nach GOZ  Rechnungsdatum {de(d('2025-04-11'))}
Professionelle Zahnreinigung 1x                     98,00 €
Kunststofffüllung 16 mehrflächig                   212,35 €
Rechnungsbetrag                                    310,35 €
Bitte überweisen Sie den Betrag bis {de(d('2025-05-09'))}.
""", kind="Rechnung", amount=310.35, direction="Ausgabe", due="2025-05-09")

# --- Fitnessstudio: Vertrag + Kündigung
add("sam", d("2024-03-01"), "FitPunkt_Vertrag.pdf", f"""
FitPunkt Studios GmbH · 45127 Essen
Mitgliedsvertrag Studio Karlsruhe-Mühlburg
Mitglied: Sam Muster, Lindenweg 12, 76137 Karlsruhe
Beginn 01.03.2024, Mindestlaufzeit 12 Monate, danach monatlich kündbar.
Monatsbeitrag 24,99 €, Servicepauschale halbjährlich 15,00 €. Einzug per Lastschrift.
""", kind="Vertrag", amount=24.99, direction="Ausgabe")
add("sam", d("2025-10-02"), "FitPunkt_Kuendigungsbestaetigung.pdf", f"""
FitPunkt Studios GmbH · 45127 Essen
{ADR_S}
{de(d('2025-10-02'))}
Kündigungsbestätigung Mitgliedsnummer 7781234
Hiermit bestätigen wir die Kündigung Ihrer Mitgliedschaft zum 31.10.2025.
""", kind="Kündigung", amount=None)

# --- Kita monatlich Bescheid jährlich
add("sam", d("2025-08-01"), "Kita_Gebuehrenbescheid.pdf", f"""
Stadt Karlsruhe · Sozial- und Jugendbehörde
{ADR_S}
Gebührenbescheid Kindertagesstätte Sonnenblume für Mia Muster
ab 01.09.2025 monatlich 186,00 € (Ganztag, inkl. Verpflegung 72,00 €).
Abbuchung jeweils zum 5. des Monats.
Gegen diesen Bescheid kann innerhalb eines Monats Widerspruch erhoben werden.
""", kind="Bescheid", amount=186.00, direction="Ausgabe", due="2025-09-01")

# --- Auto: Kauf, Inspektion, Reifen, Werkstatt
add("alex", d("2024-05-17"), "Kaufvertrag_Avero.pdf", f"""
Autohaus Süd GmbH · Ottostraße 20 · 76227 Karlsruhe
Kaufvertrag Gebrauchtfahrzeug
Käufer: Alex Muster, Lindenweg 12, 76137 Karlsruhe
Fahrzeug: Avero K4 Kombi 2.0 Diesel, Erstzulassung 03/2020, FIN DEMO0K4XLA012345, 58.200 km
Kaufpreis 21.900,00 € inkl. 19 % USt
Anzahlung 5.000,00 €, Rest über Finanzierung Avero Bank (siehe gesonderter Darlehensvertrag)
Gewährleistung 12 Monate. Ort/Datum Karlsruhe, {de(d('2024-05-17'))}
""", kind="Vertrag", amount=21900.00, direction="Ausgabe")
add("alex", d("2024-05-17"), "Darlehensvertrag_AveroBank.pdf", f"""
Avero Bank AG · 38100 Braunschweig
Darlehensvertrag Nr. 9 123 456 789
Darlehensnehmer Alex Muster
Nettodarlehensbetrag 16.900,00 €  Sollzins gebunden 4,99 % p.a.  effektiver Jahreszins 5,11 %
48 monatliche Raten à 388,72 €, erste Rate 01.07.2024, letzte Rate 01.06.2028
Gesamtbetrag 18.658,56 €
""", kind="Vertrag", amount=388.72, direction="Ausgabe")
for dd, what, amt in [("2025-06-12", "Inspektion 60.000 km inkl. Ölwechsel, Bremsflüssigkeit", 489.30), ("2025-10-28", "Winterreifen 225/50 R17 inkl. Montage", 824.00), ("2026-03-09", "Hauptuntersuchung §29 StVZO + AU", 149.00)]:
    add("alex", d(dd), f"Werkstatt_{dd}.pdf", f"""
Autohaus Süd GmbH · Werkstatt · Ottostraße 20 · 76227 Karlsruhe
{ADR_A}
Rechnung {random.randint(50000,59999)}  Datum {de(d(dd))}  Kennzeichen KA-AM 512  Avero K4 Kombi
{what}
Gesamtbetrag {eur(amt)} inkl. 19 % USt
Zahlbar sofort.
""", kind="Rechnung", amount=amt, direction="Ausgabe")

# --- Online-Shopping: verschiedene Beträge, eine Rücksendung (Gutschrift)
for dd, item, amt in [("2025-02-03", "Elektrische Zahnbürste", 89.99), ("2025-05-19", "Kinderfahrrad 16 Zoll", 239.00), ("2025-08-07", "Bücher (3 Artikel)", 41.97), ("2025-11-24", "Akkuschrauber 12 V", 119.00), ("2026-07-02", "Spielzeug-Eisenbahn", 79.99)]:
    add("sam" if "Kinder" in item or "Spielzeug" in item else "alex", d(dd), f"Marktplatz24_{dd}.pdf", f"""
Marktplatz 24 GmbH · Versandstr. 12 · 80807 München
Rechnung  Bestellnummer 302-{random.randint(1000000,9999999)}-{random.randint(1000000,9999999)}
Rechnungsdatum {de(d(dd))}
{item}    {eur(amt)}
Gesamtbetrag {eur(amt)} (inkl. 19 % USt)
Bezahlt mit Kreditkarte ****1234
""", kind="Rechnung", amount=amt, direction="Ausgabe")
add("alex", d("2025-12-02"), "Marktplatz24_Gutschrift.pdf", f"""
Marktplatz 24 GmbH · 80807 München
Gutschrift / Erstattung  Datum {de(d('2025-12-02'))}
Rücksendung: Akkuschrauber 12 V
Erstattungsbetrag 119,00 € – wird auf Kreditkarte ****1234 gutgeschrieben.
""", kind="Gutschrift", amount=119.00, direction="Einnahme (Erstattung)")

# --- Mahnung zu einer Rechnung (gleicher Betrag + Gebühr)
add("alex", d("2025-10-20"), "Mahnung_Sanitaer_Becker.pdf", f"""
Sanitär Becker Meisterbetrieb · Rintheimer Str. 9 · 76131 Karlsruhe
{ADR_A}
Zahlungserinnerung  {de(d('2025-10-20'))}
Zu unserer Rechnung Nr. 2025-0918 vom 15.09.2025 über 547,88 € konnten wir noch keinen Zahlungseingang feststellen.
Bitte überweisen Sie den offenen Betrag zzgl. Mahngebühr 5,00 € (gesamt 552,88 €) bis zum 31.10.2025.
""", kind="Mahnung", amount=552.88, direction="Ausgabe (gleiche Schuld wie Rechnung)", due="2025-10-31")

# --- Software-Abo in USD
add("alex", d("2026-01-08"), "Pixelwerk_Invoice.pdf", """
Pixelwerk Software Ltd
Invoice  Date Jan 8, 2026  Invoice No. PW-2026-0001234
Customer: Alex Muster
Foto-Abo Plus (20 GB) – annual, paid monthly
Subtotal USD 11.99   VAT 19 % USD 2.28   Total USD 14.27
""", kind="Rechnung", amount=14.27, currency="USD", direction="Ausgabe")

# --- Doppelter Scan (leicht anderer OCR-Text)
src = next(x for x in docs if x["file"] == "Rechnung_Sanitaer_Becker.pdf")
add("alex", d("2025-09-15"), "Scan_2025-09-16_0001.pdf", src["text"].replace("Spülkasten", "Spulkasten").replace("Rintheimer", "Rintheirner"), kind="Rechnung", amount=547.88, direction="Dublette")

# --- Frist bald fällig (relativ zu heute 13.11.2026)
add("alex", d("2026-11-05"), "Rechnung_Schornsteinfeger.pdf", f"""
Bezirksschornsteinfegermeister T. Ruß · 76137 Karlsruhe
{ADR_A}
Gebührenbescheid / Rechnung Feuerstättenschau  Datum {de(d('2026-11-05'))}
Feuerstättenschau Gastherme, Abgaswegeüberprüfung
Gesamtbetrag 86,45 € inkl. USt, davon Arbeitskosten 62,10 € (§ 35a EStG)
Zahlbar bis 19.11.2026.
""", kind="Rechnung", amount=86.45, direction="Ausgabe", due="2026-11-19", tax35a=62.10)
add("sam", d("2026-10-30"), "Elternbrief_Klassenfahrt.pdf", """
Grundschule am Lindenplatz · Klasse 2b
Liebe Eltern, die Klassenfahrt nach Bad Herrenalb findet vom 01.–03.12.2026 statt.
Bitte überweisen Sie den Kostenbeitrag von 95,00 € bis spätestens 20.11.2026 und geben Sie den unterschriebenen Abschnitt zurück.
""", kind="Brief", amount=95.00, direction="Ausgabe", due="2026-11-20")

# --- Für „Im Blick“ und Garantie (18.11.2026): Kaufbeleg mit Herstellergarantie, Kreditkarte mit Ablaufdatum,
# Heizungswartung (nächste in einem Jahr), Privatrechnung nach GOÄ (Erstattung offen)
add("alex", d("2025-12-12"), "Kaufbeleg_Fernseher.pdf", f"""
Elektro Lindner GmbH · Kaiserstraße 88 · 76133 Karlsruhe
Kassenbeleg / Rechnung Nr. 2025-118734  Datum {de(d('2025-12-12'))}
Kunde: Alex Muster, Lindenweg 12, 76137 Karlsruhe
1 × Fernseher OLED 55 Zoll, Modell Lumio 55X9      799,00 €
Gesamtbetrag 799,00 € inkl. 19 % USt (127,57 €)
Bezahlt mit Girokarte
Gesetzliche Gewährleistung 2 Jahre. Zusätzlich 3 Jahre Herstellergarantie ab Kaufdatum – Beleg bitte aufbewahren.
""", kind="Rechnung", amount=799.00, direction="Ausgabe")
add("sam", d("2025-12-28"), "Kreditkarte_Sam.pdf", f"""
Lindenbank AG · Kartenservice · Marktstraße 1 · 76133 Karlsruhe
{ADR_S}
Ihre Kreditkarte  Datum {de(d('2025-12-28'))}
Karteninhaberin: Sam Muster  Kartennummer **** **** **** 4821
Gültig bis 01/2027
Ihre Karte läuft Ende Januar 2027 ab. Die neue Karte schicken wir Ihnen rechtzeitig vorher zu – bitte vernichten Sie danach die alte.
""", kind="Brief")
add("alex", d("2025-12-01"), "Heizungswartung_2025.pdf", f"""
Sanitär Becker Meisterbetrieb · Rintheimer Str. 40 · 76131 Karlsruhe
{ADR_A}
Rechnung W-2025-311  Datum {de(d('2025-12-01'))}
Jährliche Wartung Brennwert-Gastherme, Abgasmessung, Reinigung Brenner
Gesamtbetrag 189,00 € inkl. 19 % USt, davon Arbeitskosten 142,80 € (§ 35a EStG)
Nächste Wartung empfohlen in 12 Monaten.
""", kind="Rechnung", amount=189.00, direction="Ausgabe", tax35a=142.80)
add("sam", d("2026-10-12"), "Arztrechnung_Hautarzt.pdf", f"""
Dr. med. Lena Hofmann · Fachärztin für Dermatologie · Hautarztpraxis am Marktplatz · 76133 Karlsruhe
Privatliquidation  Datum {de(d('2026-10-12'))}
Patientin: Sam Muster, geb. 04.06.1989
Rechnung nach der Gebührenordnung für Ärzte (GOÄ)
Behandlungstag 29.09.2026
GOÄ 1   Beratung                         2,3-fach   10,72 €
GOÄ 5   Symptombezogene Untersuchung     2,3-fach   10,72 €
GOÄ 750 Auflichtmikroskopie              2,3-fach   93,84 €
GOÄ 200 Verband                          2,3-fach    6,57 €
Auslagen                                            26,77 €
Rechnungsbetrag 148,62 €  Zahlbar innerhalb von 30 Tagen.
""", kind="Rechnung", amount=148.62, direction="Ausgabe")

# --- Optional mit dem Kalender mitwandern (Demoserver): DEMO_SHIFT=1 verschiebt alle Daten relativ zu heute
if os.environ.get("DEMO_SHIFT") == "1":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import shift
    days = shift.delta_days()
    for x in docs:
        x["text"] = shift.shift_text(x["text"], days)
        x["date"] = shift.shift_date(x["date"], days)
        if x["truth"].get("due"):
            x["truth"]["due"] = shift.shift_date(x["truth"]["due"], days)

# --- PDFs schreiben
for i, x in enumerate(docs):
    pdf = FPDF(format="A4")
    if not FONT:
        sys.exit("Keine TTF-Schrift gefunden – DEMO_FONT=/pfad/zur/schrift.ttf setzen")
    pdf.add_font("A", fname=FONT)
    pdf.set_auto_page_break(True, 15)
    pdf.add_page()
    pdf.set_font("A", size=10)
    for line in x["text"].split("\n"):
        pdf.multi_cell(0, 5.2, line, new_x="LMARGIN", new_y="NEXT")
    name = f"{i:03d}_{x['file']}"
    pdf.output(str(OUT / "pdf" / name))
    x["pdf"] = name
json.dump(docs, open(OUT / "docs.json", "w"), ensure_ascii=False, indent=1)
print(len(docs), "Dokumente")
