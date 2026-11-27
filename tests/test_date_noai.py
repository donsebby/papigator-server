"""Datum ohne KI (27.11.2026): nur eindeutige Ausstellungsdaten, im Zweifel keins. Alle Texte erfunden."""
import datetime

from app import worker
from app.db import connect, init_db
from app.processing import date_from_text


def test_labeled_and_letterhead():
    assert date_from_text("Stadtwerke Musterstadt\nRechnungsdatum: 14.05.2025\nKundennummer 4711") == "2025-05-14"
    assert date_from_text("Sam Muster\nMusterweg 3\n\nMusterstadt, den 3. März 2024\n\nSehr geehrte Damen") == "2024-03-03"
    assert date_from_text("Kassel, 07.11.23\nBetreff: Kündigung") == "2023-11-07"
    assert date_from_text("Datum\n02.01.2026\nUnser Zeichen AB-12") == "2026-01-02"
    assert date_from_text("Bescheiddatum 3O.O6.2O22 (OCR)") == "2022-06-30"      # O statt 0
    assert date_from_text("Datum: 01.02.2025\nLieferdatum: 28.01.2025\nFällig am 15.02.2025") == "2025-02-01"
    assert date_from_text("Maerz-Abrechnung\nDatum: 5. Maerz 2025") == "2025-03-05"


def test_single_plain_date():
    assert date_from_text("Arztbrief\nLaborbefund\n12.09.2024\nkeine Auffälligkeiten") == "2024-09-12"
    assert date_from_text("Zeugnis 2024-07-01 ausgestellt") == "2024-07-01"


def test_in_doubt_none():
    assert date_from_text("") is None and date_from_text(None) is None
    assert date_from_text("Ausweis\ngültig bis 14.05.2031") is None                       # Frist, in der Zukunft
    assert date_from_text("Sam Muster, geb. 01.04.1980") is None                           # Geburtsdatum
    assert date_from_text("Mitgliedsausweis gültig ab 01.01.2024") is None
    assert date_from_text("Abrechnung 01.01.2024 - 31.12.2024\nBetrag 12,00 €") is None   # zwei Daten
    assert date_from_text("Datum: 01.02.2025\nDatum: 03.02.2025") is None                 # widersprüchlich
    assert date_from_text("Geburtsdatum: 01.04.1980\nVertrag") is None
    assert date_from_text("Ihr Schreiben vom 12.02.2025") is None
    assert date_from_text("Server 10.0.0.47, Version 1.2.3") is None
    assert date_from_text("Betrag 1.234,56 € am 31.02.2025") is None                      # gibt es nicht
    future = (datetime.date.today() + datetime.timedelta(days=60)).strftime("%d.%m.%Y")
    assert date_from_text(f"Datum: {future}") is None


def _skipped_doc(admin, title, text, **extra):
    d = admin.upload(text=text, title=title).json()
    sets = ", ".join(f"{k}=?" for k in extra)
    with connect() as con:
        con.execute("UPDATE documents SET status='tagging', llm_status='pending'" + (", " + sets if sets else "")
                    + " WHERE id=?", (*extra.values(), d["id"]))
    return d["id"]


def test_worker_without_ai_sets_date(admin):
    """Kein KI-Server eingetragen (wie in den Tests): Dokument wird 'skipped' und bekommt das eindeutige Datum."""
    a = _skipped_doc(admin, "Rechnung Stadtwerke", "Stadtwerke\nRechnungsdatum: 14.05.2025\nBetrag 40,00 €")
    b = _skipped_doc(admin, "Ausweis Kopie", "Personalausweis\ngültig bis 01.01.2030")
    c = _skipped_doc(admin, "Brief ohne Datum", "Datum: 02.03.2025", no_date=1)
    for i in (a, b, c):
        assert worker.llm_one(i) is True
    da, db_, dc = (admin.get(f"/documents/{i}").json() for i in (a, b, c))
    assert da["llm_status"] == "skipped" and da["document_date"] == "2025-05-14" and "no_date" not in da["review_reasons"]
    assert db_["document_date"] is None and "no_date" in db_["review_reasons"]
    assert dc["document_date"] is None                     # „Hat kein Datum“ bleibt


def test_worker_vat16_still_flagged(admin):
    i = _skipped_doc(admin, "Werkstatt alt", "Musterstadt, 11.08.2025\nArbeitslohn 21,00\n16 % MwSt 3,36\nGesamt 24,36 €")
    worker.llm_one(i)
    d = admin.get(f"/documents/{i}").json()
    assert d["document_date"] == "2025-08-11" and "date_check" in d["review_reasons"]


def test_bestand_once(admin):
    a = _skipped_doc(admin, "Alt ohne KI", "Rechnungsdatum 01.10.2024\nBetrag")
    b = _skipped_doc(admin, "Alt mit KI", "Rechnungsdatum 02.10.2024\nBetrag")
    with connect() as con:
        con.execute("UPDATE documents SET status='done', llm_status='skipped' WHERE id=?", (a,))
        con.execute("UPDATE documents SET status='done', llm_status='done' WHERE id=?", (b,))
        con.execute("DELETE FROM settings WHERE key='date_noai_v1'")
    init_db()
    assert admin.get(f"/documents/{a}").json()["document_date"] == "2024-10-01"
    assert admin.get(f"/documents/{b}").json()["document_date"] is None      # mit KI erkannt: nicht anfassen
    with connect() as con:
        assert con.execute("SELECT value FROM settings WHERE key='date_noai_v1'").fetchone()[0] == "1"


def test_context_is_the_line():
    assert date_from_text("Gültig bis:\n14.05.2025") is None                  # Beschriftung in der Zeile darüber
    assert date_from_text("Geburtsdatum\n01.04.1980\nVersicherung") is None


def test_more_letter_forms():
    assert date_from_text("Versicherung AG\nSam Muster\nLindenweg 12\n76137 Karlsruhe\n02.02.2024\nBeitragsrechnung\n"
                          "Zeitraum 01.03.2024 – 28.02.2025") == "2024-02-02"           # unter der Anschrift
    assert date_from_text("Bescheinigung\nausgestellt am 10.11.2025\ngültig bis 14.11.2025") == "2025-11-10"
    assert date_from_text("Antrag\nKind geb. 16.08.2018\nKarlsruhe, 05.09.2018   Unterschrift: A. Muster") == "2018-09-05"
    # Zukunft zählt als Kandidat: „erste Rate“ ist dann nicht das einzige Datum
    assert date_from_text("Darlehen\nerste Rate 01.07.2024, letzte Rate 01.06.2058") is None
    # Frist- oder Zahlungszeile mit Wochentag ist kein Briefkopf „Ort, Datum“ (27.11.2026)
    assert date_from_text("Mahnung\nFällig am Dienstag, 25.11.2026") is None
    assert date_from_text("Mahnung\nRechnung vom 01.10.2026\nZahlbar am Montag, 03.11.2026") is None
