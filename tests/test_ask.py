"""Fragen an die Dokumente: Zeitangaben in der Frage begrenzen die Suche, statt selbst gesucht zu werden."""
import datetime as dt

import pytest

from app import ask, llm

TODAY = dt.date(2026, 11, 10)


@pytest.mark.parametrize("question,expected", [
    ("Gehalt der letzten sechs Monate", ("2026-05-01", "2026-11-10")),
    ("Wie viel Strom in den letzten 3 Monaten?", ("2026-08-01", "2026-11-10")),
    ("Tankquittungen der letzten 14 Tage", ("2026-10-27", "2026-11-10")),
    ("Rechnungen der vergangenen zwei Jahre", ("2024-11-01", "2026-11-10")),
    ("Was habe ich letztes Jahr an Kita bezahlt?", ("2025-01-01", "2025-12-31")),
    ("Arztrechnungen vom letzten Monat", ("2026-10-01", "2026-10-31")),
    ("Gehalt dieses Jahr", ("2026-01-01", "2026-11-10")),
    ("Alle Mahnungen seit März", ("2026-03-01", "2026-11-10")),
    ("Versicherungen seit Dezember", ("2025-12-01", "2026-11-10")),
    ("Steuerbescheide seit 2023", ("2023-01-01", "2026-11-10")),
    ("Wo ist meine Steuer-ID?", None),
    ("Lohnsteuerbescheinigung 2025", None),   # Jahreszahl allein: Dokument kann aus dem Folgejahr sein
])
def test_time_range(question, expected):
    span = ask.time_range(question, TODAY)
    assert (span and (span[0].isoformat(), span[1].isoformat())) == expected


def test_time_words_are_no_search_terms(monkeypatch):
    monkeypatch.setattr(ask, "_chat", lambda *a, **k: '["Gehaltsabrechnung", "Monate", "letzten"]')
    assert ask.keywords_for("Gehalt der letzten sechs Monate") == ["Gehaltsabrechnung", "Gehalt"]


def test_question_with_period_only_reads_documents_from_it(admin, monkeypatch):
    admin.patch("/settings", json={"llm_enabled": True, "llm_url": "http://192.168.1.50:11434/v1", "llm_model": "m"})
    ids = {}
    for month in range(1, 11):
        for year in (2025, 2026):
            d = admin.upload(text=f"Gehaltsabrechnung Auszahlungsbetrag {month}{year}",
                             title=f"Gehaltsabrechnung {month:02}/{year}").json()
            admin.patch(f"/documents/{d['id']}", json={"document_date": f"{year}-{month:02}-20"})
            ids[(year, month)] = d["id"]
    admin.upload(text="Gehaltsabrechnung ohne Datum", title="Alte Gehaltsabrechnung")   # undatiert: nicht dabei
    prompts = []

    def fake(prompt, timeout=180, purpose="tag"):
        prompts.append(prompt)
        return '["Gehaltsabrechnung"]' if len(prompts) == 1 else "Summe [Dok 1]"

    monkeypatch.setattr(ask, "_chat", fake)
    monkeypatch.setattr(ask.dt, "date", type("D", (dt.date,), {"today": staticmethod(lambda: TODAY)}))
    r = admin.post("/ask", json={"question": "Gehalt der letzten sechs Monate"}).json()
    assert r["range"] == {"from": "2026-05-01", "to": "2026-11-10"}
    got = [s["document_date"] for s in r["sources"]]
    assert got == [f"2026-{m:02}-20" for m in range(5, 11)]   # Mai bis Oktober 2026, chronologisch
    assert "Heute ist der 10.11.2026" in prompts[1] and "2025-" not in prompts[1]
    assert "Auszahlungsbetrag 102026" in prompts[1]               # kurze Dokumente komplett


def test_wrong_sum_is_recalculated():
    answer = ("Hier die Netto-Beträge:\n\n- Mai 2026: 2950,40 [Dok 1]\n- Juni 2026: 3.012,15 € [Dok 2]\n"
              "- Juli 2026: 2981,77 [Dok 3]\n\nSumme: 9.144,32 €")
    assert ask.check_sum(answer).endswith("Summe: 8.944,32 € (vom Server nachgerechnet)")
    right = answer.replace("9.144,32", "8.944,32")
    assert ask.check_sum(right) == right
    # mehrere Beträge in einer Zeile (Brutto/Netto): nicht raten
    unclear = "- Mai: Brutto 4.800,00, Netto 2950,40\n- Juni: Brutto 4.800,00, Netto 3012,15\nSumme: 1,00"
    assert ask.check_sum(unclear) == unclear
    assert ask.check_sum("Keine Summe hier: 12,00") == "Keine Summe hier: 12,00"
