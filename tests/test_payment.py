"""Bezahlen per QR: EPC-QR nach EPC069-12 v3.1 (Beispiele gleich in v3.0), IBAN aus dem Text (Prüfziffer), nur offene fremde Rechnungen."""
import json

import pytest

from app import payment
from app.bank import _num_hash
from app.db import connect

# Beispiel-IBANs aus Spezifikationen und Bankdokumentationen (keine echten Konten von Personen)
IBAN = "DE89370400440532013000"
IBAN2 = "DE02120300000000202051"


# ---------- EPC-QR gegen die Beispiele der Spezifikation (EPC069-12 v3.1 = v3.0, Abschnitt 2.3) ----------

def test_epc_example_v1():
    """V1: 95 Zeichen mit 9 Zeilenumbrüchen, 96 Byte UTF-8 (ä)."""
    p = payment.epc_payload("Franz Mustermänn", "DE71110220330123456789", 12.3, bic="BHBLDEHHXXX", purpose="GDDS",
                            reference="RF18539007547034", version="001", charset="1")
    assert p == ("BCD\n001\n1\nSCT\nBHBLDEHHXXX\nFranz Mustermänn\nDE71110220330123456789\nEUR12.3\nGDDS\n"
                 "RF18539007547034")
    assert len(p) == 95 and p.count("\n") == 9 and len(p.encode("utf-8")) == 96


def test_epc_example_v2():
    """V2: ohne BIC, Zweck und Referenz leer, Text als letztes Element – 103 Zeichen, 10 Umbrüche, 103 Byte ISO 8859-1."""
    p = payment.epc_payload("François D'Alsace S.A.", "FR1420041010050500013M02606", 12.3,
                            text="Client:Marie Louise La Lune", version="002", charset="2")
    assert p == ("BCD\n002\n2\nSCT\n\nFrançois D'Alsace S.A.\nFR1420041010050500013M02606\nEUR12.3\n\n\n"
                 "Client:Marie Louise La Lune")
    assert len(p) == 103 and p.count("\n") == 10 and len(p.encode("latin-1")) == 103


def test_epc_rules():
    with pytest.raises(ValueError):
        payment.epc_payload("A", IBAN, 1, reference="RF18539007547034", text="beides")
    assert not payment.epc_payload("A", IBAN, 1).endswith("\n")           # kein Trennzeichen nach dem letzten Element
    assert payment.format_amount(12.30) == "EUR12.3"                      # wie im Beispiel der Spezifikation
    assert payment.format_amount(1) == "EUR1"
    assert payment.format_amount(0.01) == "EUR0.01"
    assert payment.format_amount(999999999.99) == "EUR999999999.99"
    assert payment.format_amount(19.999) == "EUR20"


# ---------- IBAN ----------

def test_iban_checksum():
    assert payment.iban_valid(IBAN) and payment.iban_valid("DE71110220330123456789")
    assert payment.iban_valid("FR1420041010050500013M02606") and payment.iban_valid("de89 3704 0044 0532 0130 00")
    assert not payment.iban_valid("DE89370400440532013001")              # Prüfziffer falsch
    assert not payment.iban_valid("DE8937040044053201300")               # zu kurz
    assert not payment.iban_valid("US89370400440532013000")              # kein SEPA-Land


def test_find_ibans_in_text():
    text = (f"Bitte überweisen Sie auf IBAN: DE89 3704 0044 0532 0130 00, BIC COBADEFFXXX.\n"
            f"Alternativ {IBAN2.lower()} – Ihre Referenz DE893704004405320130001 (zu lang) und DE89 3704 0044 0532 0130 01.")
    assert payment.find_ibans(text) == [IBAN, IBAN2]
    assert payment.find_ibans(f"IBAN {IBAN} und nochmal {IBAN[:4]} {IBAN[4:]}") == [IBAN]   # doppelt zählt einmal
    assert payment.find_ibans("Ende 2026 DE12") == []


def test_bic_only_with_label_and_matching_country():
    assert payment.find_bic("BIC: COBADEFFXXX", IBAN) == "COBADEFFXXX"
    assert payment.find_bic("SWIFT-Code cobadeff", IBAN) == "COBADEFF"
    assert payment.find_bic("RECHNUNG COBADEFFXXX", IBAN) is None         # ohne Beschriftung
    assert payment.find_bic("BIC: BNPAFRPPXXX", IBAN) is None             # anderes Land als die IBAN
    assert payment.find_bic("BIC: COBADEFFXXX oder BIC: DEUTDEFFXXX", IBAN) is None


# ---------- Endpunkt ----------

def _invoice(api, text=None, **fields):
    api.patch("/auth/me", json={"pay_qr": True})   # standardmäßig aus – für diese Tests einschalten
    text = text if text is not None else f"Rechnung Nr. 4711\nBetrag 49,90 EUR\nIBAN: DE89 3704 0044 0532 0130 00\nBIC: COBADEFFXXX"
    doc = api.upload(text=text, title=fields.pop("title", "Rechnung Stadtwerke")).json()
    vals = {"sender": "Stadtwerke Musterstadt", "amount": 49.9, "currency": "EUR", "doc_kind": "rechnung",
            "direction": "out", "autopay": 0, "status": "done",
            "refs": json.dumps({"invoice": "4711", "customer": "K-123"}), **fields}
    with connect() as con:
        con.execute(f"UPDATE documents SET {', '.join(f'{k}=?' for k in vals)} WHERE id=?", (*vals.values(), doc["id"]))
    return doc["id"]


def test_payment_offered(admin):
    doc = _invoice(admin)
    r = admin.get(f"/documents/{doc}/payment")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p.pop("sources") == {"iban": "IBAN: DE89 3704 0044 0532 0130 00", "amount": "Betrag 49,90 EUR"}
    assert p == {"iban": IBAN, "bic": "COBADEFFXXX", "name": "Stadtwerke Musterstadt", "amount": 49.9, "currency": "EUR",
                 "reference": "Rechnung 4711, Kunde K-123",
                 "epc": "BCD\n002\n1\nSCT\nCOBADEFFXXX\nStadtwerke Musterstadt\nDE89370400440532013000\nEUR49.9\n\n\n"
                        "Rechnung 4711, Kunde K-123"}


def test_reference_falls_back_to_title(admin):
    doc = _invoice(admin, refs=None, title="Wasser 2026")
    assert admin.get(f"/documents/{doc}/payment").json()["reference"] == "Wasser 2026"


def test_other_users_document_is_404(admin, sam):
    doc = _invoice(admin)
    assert sam.get(f"/documents/{doc}/payment").status_code == 404
    assert admin.put("/shares", json={"user_id": sam.id, "can_edit": False}).status_code == 200
    assert sam.get(f"/documents/{doc}/payment").status_code == 404        # Sam hat es nicht eingeschaltet
    sam.patch("/auth/me", json={"pay_qr": True})
    assert sam.get(f"/documents/{doc}/payment").status_code == 200        # sehen reicht, wie beim Dokument


@pytest.mark.parametrize("fields", [
    {"autopay": 1},                                  # Lastschrift
    {"due_date": "2030-01-01", "due_done": 1},       # Frist erledigt
    {"direction": "in"},                             # Einnahme
    {"doc_kind": "kontoauszug"},
    {"doc_kind": "gutschrift", "direction": None},
    {"doc_kind": "vertrag", "direction": None},      # ohne Frist und keine Rechnung
    {"amount": 0},
    {"amount": None},
    {"currency": "USD"},
    {"sender": None},
    {"deleted_at": "2026-11-01 10:00:00"},
])
def test_not_offered(admin, fields):
    doc = _invoice(admin, **fields)
    assert admin.get(f"/documents/{doc}/payment").status_code == 404


def test_offered_variants(admin):
    # Mahnung (bewegt laut extras.flow kein Geld, ist aber zu bezahlen) und Vertrag mit offener Frist
    assert admin.get(f"/documents/{_invoice(admin, doc_kind='mahnung', direction=None, title='M')}/payment").status_code == 200
    assert admin.get(f"/documents/{_invoice(admin, doc_kind='vertrag', direction='out', due_date='2030-01-01', title='V')}/payment").status_code == 200
    assert admin.get(f"/documents/{_invoice(admin, currency=None, title='C')}/payment").status_code == 200


def test_needs_exactly_one_foreign_iban(admin):
    none = _invoice(admin, text="Rechnung ohne Bankverbindung", title="A")
    two = _invoice(admin, text=f"IBAN {IBAN}\noder IBAN {IBAN2}", title="B")
    bad = _invoice(admin, text="IBAN DE89 3704 0044 0532 0130 01", title="C")
    for d in (none, two, bad):
        assert admin.get(f"/documents/{d}/payment").status_code == 404


def test_own_account_is_not_a_payee(admin):
    """Steht die eigene IBAN mit im Brief, bleibt die fremde; ist nur die eigene da, gibt es nichts zu bezahlen."""
    with connect() as con:
        con.execute("INSERT INTO bank_accounts(name, owner_id, number_hash) VALUES ('Giro', ?, ?)",
                    (admin.id, _num_hash(IBAN2)))
    both = _invoice(admin, text=f"Ihr Konto: {IBAN2}\nZahlbar auf IBAN {IBAN}", title="A")
    p = admin.get(f"/documents/{both}/payment").json()
    assert p["iban"] == IBAN and p["bic"] is None
    only_own = _invoice(admin, text=f"Wir buchen von Ihrem Konto {IBAN2} ab", title="B")
    assert admin.get(f"/documents/{only_own}/payment").status_code == 404


def test_own_account_by_plain_account_number(admin):
    """Bank-CSVs nennen oft nur die Kontonummer – die passt auch zur IBAN."""
    with connect() as con:
        con.execute("INSERT INTO bank_accounts(name, owner_id, number_hash) VALUES ('Giro', ?, ?)",
                    (admin.id, _num_hash("532013000")))
    doc = _invoice(admin)
    assert admin.get(f"/documents/{doc}/payment").status_code == 404


def test_long_values_fit_the_qr(admin):
    doc = _invoice(admin, sender="S" * 120, refs=None, title="T" * 200)
    p = admin.get(f"/documents/{doc}/payment").json()
    assert len(p["name"]) == 70 and len(p["epc"].encode()) <= 331
    assert "\n" not in p["reference"] and len(p["reference"]) <= 140



def test_off_by_default_and_per_account(admin, sam):
    doc = _invoice(admin)
    admin.patch("/auth/me", json={"pay_qr": False})
    r = admin.get(f"/documents/{doc}/payment")
    assert r.status_code == 404 and "ausgeschaltet" in r.json()["detail"]
    assert admin.get("/auth/me").json()["pay_qr"] is False
    assert sam.get("/auth/me").json()["pay_qr"] is False                 # neue Konten: aus
    admin.patch("/auth/me", json={"pay_qr": True})
    assert admin.get(f"/documents/{doc}/payment").status_code == 200


def test_iban_needs_a_label_nearby(admin):
    unlabeled = "Rechnung Nr. 4711\nBetrag 49,90 EUR\nBitte überweisen Sie auf DE89 3704 0044 0532 0130 00"
    assert admin.get(f"/documents/{_invoice(admin, text=unlabeled)}/payment").status_code == 404
    labeled = "Rechnung Nr. 4711\nBetrag 49,90 EUR\nUnsere Bankverbindung:\nDE89 3704 0044 0532 0130 00"
    assert admin.get(f"/documents/{_invoice(admin, text=labeled, title='Zweite')}/payment").status_code == 200


def test_amount_source_missing_when_changed_by_hand(admin):
    doc = _invoice(admin, amount=59.9)                                   # steht so nicht im Brief
    p = admin.get(f"/documents/{doc}/payment").json()
    assert p["sources"]["amount"] is None and p["sources"]["iban"].startswith("IBAN:")
    assert payment.amount_snippet(1234.5, "Gesamtbetrag 1.234,50 € inkl. USt") == "Gesamtbetrag 1.234,50 € inkl. USt"
