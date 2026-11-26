"""Englische Fristnotizen (Erkennung auf Englisch) werden wie die deutschen behandelt: Lastschrift, Auszahlung,
Gültigkeit, Kündigung."""
from datetime import date, timedelta

import pytest

from app import extras, processing
from app.db import connect


def _apply(doc_id, data):
    with connect() as con:
        processing.apply_llm(con, doc_id, data)
        return dict(con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())


@pytest.mark.parametrize("note,payment", [
    ("Payment due", True), ("Zahlungsziel", True), ("Direct debit", True), ("Instalment", True),
    ("Notice period contract", False), ("Cancellation deadline", False), ("Kündigungsfrist", False),
    ("Payout", False), ("Payslip", False), ("Amount due", True),
    ("Objection due", False), ("Final payment notice", True), ("Termin Installateur", False),
])
def test_payment_note(note, payment):
    assert processing._due_is_payment(note) is payment


def test_direct_debit_drops_payment_deadline_but_not_notice(admin):
    soon_d = date.today() + timedelta(days=10)
    soon, de = soon_d.isoformat(), soon_d.strftime("%d.%m.%Y")
    d = admin.upload(f"Phone bill, the amount will be debited on {de}", "Phone").json()["id"]
    assert _apply(d, {"autopay": True, "due_date": soon, "due_note": "Payment due"})["due_date"] is None
    d2 = admin.upload(f"Membership fee. Notice possible until {de}", "Club").json()["id"]
    row = _apply(d2, {"autopay": True, "due_date": soon, "due_note": "Notice period"})
    assert row["due_date"] == soon


def test_german_notes_not_caught_by_english_words(admin):
    """Englische Wortstämme dürfen deutsche Notizen nicht umdeuten („Mietwagen“ ≠ wage, „Installateur“ ≠ instalment)."""
    soon_d = date.today() + timedelta(days=10)
    soon, de = soon_d.isoformat(), soon_d.strftime("%d.%m.%Y")
    txt = f"Bitte bis {de} erledigen. Termin mit dem Installateur, Mietwagen, ungültige Angabe."
    for note in ("Mietwagen bezahlen", "Termin Installateur", "Invalidenausweis verlängern"):
        d = admin.upload(txt + note, note).json()["id"]
        assert _apply(d, {"due_date": soon, "due_note": note})["due_date"] == soon, note
    # Widerspruch mit „due“ ist keine Zahlung: auch bei Lastschrift bleibt die Frist
    d = admin.upload(f"Objection possible until {de}", "Objection").json()["id"]
    assert _apply(d, {"autopay": True, "due_date": soon, "due_note": "Objection due"})["due_date"] == soon
    assert extras._cancel_by([{"id": 1, "due_date": soon, "due_note": "Final payment notice", "doc_kind": None}],
                             "jährlich", date.today()) is None


def test_nothing_to_do_notes(admin):
    txt = ("Citizens office, 10.11.2026. Your ID card (valid until 09.11.2036) has arrived. "
           "Please pick it up by 15.01.2027.")
    d = admin.upload(txt, "ID card").json()["id"]
    assert _apply(d, {"document_date": "2026-11-10", "due_date": "2036-11-09", "due_note": "ID card valid until"})["due_date"] is None
    assert _apply(d, {"document_date": "2026-11-10", "due_date": "2027-01-15", "due_note": "Pick up ID card"})["due_date"] == "2027-01-15"
    for note, keep in (("Transfer of refund", False), ("Salary payment", False), ("Salary payout", False),
                       ("Membership fee payment", True), ("Renew passport", True), ("Passport expires", False)):
        dn = admin.upload(txt + " " + note, "Case " + note).json()["id"]
        row = _apply(dn, {"document_date": "2026-11-10", "due_date": "2027-01-15", "due_note": note})
        assert (row["due_date"] == "2027-01-15") is keep, note


def test_cancel_deadline_in_contracts():
    today = date(2026, 11, 16)
    due = lambda i, d, n: {"id": i, "due_date": d, "due_note": n, "doc_kind": None}
    assert extras._cancel_by([due(1, "2026-11-30", "Notice period contract")], "jährlich", today)["date"] == "2026-11-30"
    assert extras._cancel_by([due(1, "2025-10-31", "Cancellation deadline")], "jährlich", today)["date"] == "2027-10-31"
    # Sonderkündigungsrecht („special termination right“) gilt nur einmal
    assert extras._cancel_by([due(1, "2025-10-31", "Special termination right")], "jährlich", today) is None
    assert extras._cancel_by([due(1, "2026-12-01", "Payment due")], "jährlich", today) is None
