"""Demo-Daten (demo/): Der Demoserver befüllt sich jeden Tag neu und geht nur online, wenn die Prüfung in seed.py
passt. Hier für viele Tage im Jahr: erkennt Papigator aus den erfundenen Umsätzen genau die eine Preiserhöhung
(Klangwerk), keine Scheinabos aus Einkäufen mit schwankenden Beträgen, und bleibt „Einsortieren“ überschaubar?"""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parent.parent / "demo"
sys.path.insert(0, str(DEMO))
import make_bank  # noqa: E402
import shift  # noqa: E402

from app import bank  # noqa: E402
from app.auth import User  # noqa: E402
from app.db import connect  # noqa: E402

META = json.loads((DEMO / "meta.json").read_text())
DAYS = [dt.date(2026, 11, 16) + dt.timedelta(days=n) for n in range(0, 31, 3)] + [
    dt.date(2027, 1, 1), dt.date(2027, 2, 28), dt.date(2028, 2, 29), dt.date(2027, 3, 31), dt.date(2027, 8, 3),
    dt.date(2027, 8, 4), dt.date(2027, 8, 5), dt.date(2027, 12, 31), dt.date(2027, 5, 15)]


@pytest.mark.parametrize("today", DAYS, ids=str)
def test_demo_bank_data_reads_well_every_day(admin, today):
    days = (today - shift.BASE).days
    docs = [{"file": m["file"], "amount": m.get("amount"), "date": shift.shift_date(m.get("document_date"), days)}
            for m in META]
    data = make_bank.exports(today, docs)["alex"]
    pre = admin.post("/bank/import", files={"file": ("demo.csv", data, "text/csv")}, data={"preview": "true"}).json()
    assert {a["name"] for a in pre["accounts"]} == {"Gemeinschaftskonto", "Urlaub", "Jährlich", "Sparkonto Alex"}
    r = admin.post("/bank/import", files={"file": ("demo.csv", data, "text/csv")},
                   data={"preview": "false", "shares": json.dumps({a["key"]: False for a in pre["accounts"]})})
    assert r.status_code == 200, r.text
    with connect() as con:
        user = User.from_row(con.execute("SELECT * FROM users WHERE id=?", (admin.id,)).fetchone())
        found = {(i["type"], i["key"]) for i in bank.insights(con, user, today=today)}
    assert found == {("price", "klangwerk music")}, found
    to_sort = len(admin.get("/bank/to_sort").json()["transactions"])
    assert 1 <= to_sort <= 15, to_sort



@pytest.mark.parametrize("today", DAYS[::3], ids=str)
def test_demo_yearly_costs(admin, today):
    """Jährliche Kosten im Demo-Haushalt: genau die erfundenen Jahresposten (Kfz, Haftpflicht, Autoclub, Kfz-Steuer,
    Pixelplay, Domain aus dem Topf), der Topf „Jährlich“ wird erkannt, keine Scheinposten aus Monatlichem oder Einkäufen."""
    from app import yearly
    days = (today - shift.BASE).days
    docs = [{"file": m["file"], "amount": m.get("amount"), "date": shift.shift_date(m.get("document_date"), days)}
            for m in META]
    data = make_bank.exports(today, docs)["alex"]
    pre = admin.post("/bank/import", files={"file": ("demo.csv", data, "text/csv")}, data={"preview": "true"}).json()
    admin.post("/bank/import", files={"file": ("demo.csv", data, "text/csv")},
               data={"preview": "false", "shares": json.dumps({a["key"]: False for a in pre["accounts"]})})
    with connect() as con:
        user = User.from_row(con.execute("SELECT * FROM users WHERE id=?", (admin.id,)).fetchone())
        y = yearly.overview(con, user, today=today)
    names = {(i["label"] or i["name"]).lower() for i in y["items"] if i["in_sum"] and i["active"]}
    want = ["hanse", "lindwurm", "autoclub", "kfz-steuer", "pixelplay", "domain"]
    for w in want:
        assert any(w in n for n in names), (w, names)
    assert len(names) == len(want), names
    assert y["pot"] and y["pot"]["name"] == "Jährlich" and y["pot"]["monthly"] == 8500


def test_demo_meta_kinds_recipients_refs():
    """meta.json (ohne fpdf2 prüfbar): jede Art ist ein bekannter Code, Nummern nur mit bekannten Schlüsseln, die Arten
    ohne Geld sind vertreten und tragen keinen Betrag, und „Zu prüfen“ bleibt klein (seed.py bricht bei mehr als 6 ab)."""
    from app.processing import KIND_LABELS, REF_KEYS
    assert len({m["file"] for m in META}) == len(META)
    assert [m["i"] for m in META] == list(range(len(META)))
    for m in META:
        assert m["doc_kind"] in KIND_LABELS, m["file"]
        assert set(m.get("refs") or {}) <= set(REF_KEYS), m["file"]
        assert all(isinstance(v, str) and v for v in (m.get("refs") or {}).values()), m["file"]
    no_money = [m for m in META if m["doc_kind"] in ("zeugnis", "urkunde", "bescheinigung", "befund", "antrag", "ausweis",
                                                       "bericht", "bestaetigung")]
    assert {m["doc_kind"] for m in no_money} == {"zeugnis", "urkunde", "bescheinigung", "befund", "antrag", "ausweis",
                                                 "bericht", "bestaetigung"}
    assert all(m.get("amount") is None and not m.get("recurring") and m.get("tags") for m in no_money)
    assert sum(1 for m in META if not m.get("document_date") or not m.get("tags")) <= 4
    assert sum(1 for m in META if m.get("recipient")) >= 40 and sum(1 for m in META if m.get("refs")) >= 30


def test_shift_keeps_months_apart():
    """Monatsnamen wandern um ganze Monate – zwei verschiedene Monate werden nie zum selben (sonst Dubletten beim Seed)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "demo"))
    import shift
    months = [f"{m} 2025" for m in shift.MONTHS]
    for days in (0, 16, 30, 45, 100, 200, 400):
        out = [shift.shift_text(m, days) for m in months]
        assert len(set(out)) == 12, (days, out)
    assert shift.shift_text("Abrechnung für Februar 2025", 10) == "Abrechnung für Februar 2025"
    assert shift.shift_text("Abrechnung für Februar 2025", 16) == "Abrechnung für März 2025"
    assert shift.shift_text("Abrechnung für Dezember 2025", 31) == "Abrechnung für Januar 2026"
