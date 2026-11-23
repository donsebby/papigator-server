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
    assert {a["name"] for a in pre["accounts"]} == {"Gemeinschaftskonto", "Urlaub", "Sparkonto Alex"}
    r = admin.post("/bank/import", files={"file": ("demo.csv", data, "text/csv")},
                   data={"preview": "false", "shares": json.dumps({a["key"]: False for a in pre["accounts"]})})
    assert r.status_code == 200, r.text
    with connect() as con:
        user = User.from_row(con.execute("SELECT * FROM users WHERE id=?", (admin.id,)).fetchone())
        found = {(i["type"], i["key"]) for i in bank.insights(con, user, today=today)}
    assert found == {("price", "klangwerk music")}, found
    to_sort = len(admin.get("/bank/to_sort").json()["transactions"])
    assert 1 <= to_sort <= 15, to_sort
