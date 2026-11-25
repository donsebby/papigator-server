"""Kündigungshelfer: Nummern aus den Vertragsdokumenten, Schreiben als PDF (lesbar, mit Umlauten), auf Wunsch abgelegt."""
import io

from pypdf import PdfReader


def test_prefill_reads_numbers_only_from_visible_documents(admin, sam):
    d = admin.upload("HanseKraft Versicherung AG Beitragsrechnung Versicherungsschein-Nr.: KF-4711-23 "
                     "Kundennummer: 0815-42 Bitte zahlen Sie bis", "Beitragsrechnung Kfz 2026").json()
    with __import__("app.db", fromlist=["connect"]).connect() as con:
        con.execute("UPDATE documents SET sender='HanseKraft Versicherung AG' WHERE id=?", (d["id"],))
    r = admin.get("/contracts/cancel-prefill", params={"document_ids": str(d["id"])}).json()
    assert r == {"to_name": "HanseKraft Versicherung AG", "contract_number": "KF-4711-23", "customer_number": "0815-42",
                 "about": "Beitragsrechnung Kfz"}
    assert sam.get("/contracts/cancel-prefill", params={"document_ids": str(d["id"])}).json()["to_name"] is None


def test_letter_pdf_and_save(admin):
    body = {"from_name": "Alex Muster", "from_address": "Gartenweg 3\n12345 Musterstadt", "to_name": "Stromwerk Süd GmbH",
            "to_address": "Postfach 10 20\n54321 Beispielstadt", "about": "Stromliefervertrag", "contract_number": "SW-99",
            "by_date": "2026-12-31", "place": "Musterstadt"}
    r = admin.post("/contracts/cancel-letter", json=body)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    text = PdfReader(io.BytesIO(r.content)).pages[0].extract_text()
    for want in ("Kündigung – Stromliefervertrag", "Vertragsnummer: SW-99", "Stromwerk Süd GmbH",
                 "spätestens zum 31.12.2026", "Mit freundlichen Grüßen", "Gartenweg 3"):
        assert want in text, want
    saved = admin.post("/contracts/cancel-letter", json={**body, "save": True}).json()
    doc = admin.get(f"/documents/{saved['document_id']}").json()
    assert doc["title"] == "Kündigung Stromwerk Süd GmbH" and doc["sender"] == "Stromwerk Süd GmbH"
    assert admin.post("/contracts/cancel-letter", json={**body, "from_name": " "}).status_code == 400
    assert admin.post("/contracts/cancel-letter", json={**body, "by_date": "morgen"}).status_code == 400
