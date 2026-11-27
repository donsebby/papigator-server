"""Funde aus der Stichprobe vom 27.11.2026 (echte Ablage, nur unverfängliche Dokumente): Empfänger aus dem Briefkopf
des Absenders, Nummern des Absenders, § 35a beim Schornsteinfeger, HU-Termin auf Werkstattrechnungen, OCR-Jahr bei
16 % MwSt – für neue Dokumente und einmal über den Bestand. Alle Texte erfunden."""
from app import processing
from app.db import connect

WORKSHOP = """KFZ-MEISTERBETRIEB Auto-Werner
Rolf Wernerl · Hauptstraße 5 · 90000 Musterstadt
St.Nr.: 241/123/45611   Ust.ID.Nr.: DE 123456789
Frau Alex Muster
Kennzeichen: N-AB 123  HU: 11.2026
Rechnungsdatum: 30.04.2026  Rechnungsnummer: 2604117
Ölwechsel 94,60 €  zzgl. MwSt. 19 % 47,84 €  Gesamtbetrag: 299,64 €"""

SWEEP = """Kaminkehrermeister Sam Ruß · 90001 Musterdorf
Herrn Alex Muster
Rechnung-Nr.: 2025004   SteuerNr.: 234/567/89056   Kd.Nr: 4711326   Datum: 01.02.2025
Kehr- und Überprüfungsgebühren 34,58  Abgaswegeüberprüfung 35,00
Rechnungsbetrag € 82,80"""

OLD_VAT = """Rechnung  Rechnungsdatum: 11.08.2025
Standlichtbirne 3,00 €  Motoröl 18,00 €  zzgl. MwSt. 16 % 3,36 €  Gesamtbetrag: 24,36 €"""


def _doc(admin, text, data, title="Dok"):
    i = admin.upload(text, title).json()["id"]
    with connect() as con:
        processing.apply_llm(con, i, data)
        return i, dict(con.execute("SELECT * FROM documents WHERE id=?", (i,)).fetchone())


def test_recipient_is_never_the_sender():
    assert processing.same_party("Rolf Wernerl", "Auto-Wernerl")
    assert processing.same_party("Herr Wernel", "Auto-Wernerl")             # Texterkennung: ein Buchstabe anders
    assert not processing.same_party("Alex Muster", "Auto-Wernerl")
    assert not processing.same_party("Familie Muster", "Familienkasse")
    assert processing.recipient_from({"recipient": "Rolf Wernerl", "sender": "Auto-Werner"}, WORKSHOP) is None
    assert processing.recipient_from({"recipient": "Alex Muster", "sender": "Auto-Werner"}, WORKSHOP) == "Alex Muster"


def test_sender_numbers_are_no_references():
    refs = processing.refs_from({"references": {"other": "234/567/89056", "customer": "4711326", "invoice": "2025004"}},
                                SWEEP, "rechnung")
    assert refs == {"customer": "4711326", "invoice": "2025004"}
    assert processing.refs_from({"references": {"other": "DE 123456789"}}, WORKSHOP, "rechnung") is None
    court = "Amtsgericht Musterstadt, Az. 12 C 345/26\nSitz: Musterstadt, Amtsgericht Musterstadt HRB 5919"
    assert processing.refs_from({"references": {"case": "12 C 345/26", "other": "HRB 5919"}}, court, "info") == \
        {"case": "12 C 345/26"}
    # im Steuerbescheid ist die Steuernummer meine
    assert processing.refs_from({"references": {"case": "234/567/89056"}}, SWEEP, "bescheid") == {"case": "234/567/89056"}


def test_new_workshop_invoice(admin):
    _, d = _doc(admin, WORKSHOP, {"kind": "Rechnung", "sender": "Auto-Werner", "recipient": "Rolf Wernerl",
                                  "document_date": "2026-04-30", "amount": 299.64, "direction": "Ausgabe",
                                  "references": {"other": "241/123/45611", "invoice": "2604117"}})
    assert d["recipient"] is None and d["refs"] == '{"invoice": "2604117"}'
    assert d["service_what"] == "Hauptuntersuchung" and d["service_next"] == "2026-11-30"
    assert d["date_check"] == 0 and d["tax_35a"] is None


def test_hu_only_after_document_date():
    assert processing.hu_from_text("HU: 03.2026", "2026-04-30") is None
    assert processing.hu_from_text("nächste HU 3/27", "2026-04-30") == "2027-03-31"
    assert processing.hu_from_text("HSN: 3001 TSN: AHM", "2026-04-30") is None


def test_sweep_counts_for_35a(admin):
    _, d = _doc(admin, SWEEP, {"kind": "Rechnung", "sender": "Sam Ruß", "document_date": "2025-02-01",
                               "amount": 82.80, "direction": "Ausgabe"})
    assert d["tax_35a"] == 82.80
    _, d = _doc(admin, SWEEP + "\nFeuerstättenschau", {"kind": "Rechnung", "amount": 82.80, "direction": "Ausgabe"},
                "Feuerstättenschau")
    assert d["tax_35a"] is None


def test_vat16_flags_date(admin):
    assert processing.vat_date_doubtful("2025-08-11", OLD_VAT)
    assert not processing.vat_date_doubtful("2020-08-11", OLD_VAT)
    assert not processing.vat_date_doubtful("2021-02-01", OLD_VAT)            # Rechnung über Leistungen aus 2020
    assert not processing.vat_date_doubtful("2006-05-01", OLD_VAT)            # vor 2007 galten 16 %
    assert not processing.vat_date_doubtful("2025-08-11", WORKSHOP)
    i, d = _doc(admin, OLD_VAT, {"kind": "Rechnung", "document_date": "2025-08-11", "amount": 24.36})
    assert d["document_date"] == "2025-08-11" and d["date_check"] == 1
    with connect() as con:
        processing.recheck_flagged_dates(con)                                # beim Start: bleibt markiert
        assert con.execute("SELECT date_check FROM documents WHERE id=?", (i,)).fetchone()[0] == 1
    review = admin.get("/documents?review=true").json()
    assert i in [x["id"] for x in review]
    admin.patch(f"/documents/{i}", json={"document_date": "2020-08-11"})     # von Hand korrigiert
    with connect() as con:
        assert con.execute("SELECT date_check FROM documents WHERE id=?", (i,)).fetchone()[0] == 0


def test_bestand_once(admin):
    ids = [admin.upload(t, f"Alt {n}").json()["id"] for n, t in enumerate((WORKSHOP, SWEEP, OLD_VAT))]
    with connect() as con:
        con.execute("UPDATE documents SET status='done', llm_status='done', sender='Auto-Werner', recipient='Rolf Wernerl', "
                    "refs='{\"other\": \"241/123/45611\", \"invoice\": \"2604117\"}', doc_kind='rechnung', "
                    "document_date='2026-04-30' WHERE id=?", (ids[0],))
        con.execute("UPDATE documents SET status='done', llm_status='done', sender='Sam Ruß', recipient='Alex Muster', "
                    "doc_kind='rechnung', direction='out', amount=82.8, document_date='2025-02-01' WHERE id=?", (ids[1],))
        con.execute("UPDATE documents SET status='done', document_date='2025-08-11', date_check=0 WHERE id=?", (ids[2],))
        n = processing.recheck_v2(con)
        rows = {r["id"]: dict(r) for r in con.execute(
            f"SELECT * FROM documents WHERE id IN ({','.join('?' * len(ids))})", ids)}
    assert n == {"recipient": 1, "refs": 1, "tax_35a": 1, "hu": 1, "date_check": 1}
    w, s, v = (rows[i] for i in ids)
    assert w["recipient"] is None and w["llm_status"] == "refill_k" and w["refs"] == '{"invoice": "2604117"}'
    assert w["service_next"] == "2026-11-30"
    assert s["recipient"] == "Alex Muster" and s["tax_35a"] == 82.8
    assert v["date_check"] == 1


def test_household_name_stays_recipient():
    """„Sam Beispiel“ bekommt Post von „Beispiel Elektro GmbH“: bleibt Empfänger, weil Sam zum Haushalt gehört (27.11.2026)."""
    text = "Beispiel Elektro GmbH\nSam Beispiel\nMusterweg 1\nRechnung"
    data = {"recipient": "Sam Beispiel", "sender": "Beispiel Elektro GmbH"}
    assert processing.recipient_from(data, text) is None                                  # ohne Haushaltswissen wie bisher
    assert processing.recipient_from(data, text, household=["Sam Beispiel"]) == "Sam Beispiel"
    assert processing.recipient_from({"recipient": "Rolf Wernerl", "sender": "Auto-Werner"}, WORKSHOP,
                                     household=["Sam Beispiel"]) is None                   # Inhaber bleibt draußen


def test_household_from_other_letters(admin):
    """Haushalt erkannt, weil „Sam Beispiel“ schon Post von zwei anderen Absendern bekam."""
    for snd in ("Stadtwerke", "Krankenkasse"):
        i = admin.upload(f"{snd}\nSam Beispiel", f"Brief {snd}").json()["id"]
        admin.patch(f"/documents/{i}", json={"sender": snd})
        with connect() as con:
            con.execute("UPDATE documents SET recipient='Sam Beispiel', hand_fields=NULL WHERE id=?", (i,))
    text = "Beispiel Elektro GmbH\nSam Beispiel\nMusterweg 1\nRechnung"
    _, row = _doc(admin, text, {"recipient": "Sam Beispiel", "sender": "Beispiel Elektro GmbH", "doc_kind": "rechnung"})
    assert row["recipient"] == "Sam Beispiel"
