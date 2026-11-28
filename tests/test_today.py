"""Zusatzkarten für „Heute“ (app/today.py, 28.11.2026): Kündigen möglich, Steuerjahr, Briefkasten, vor einem Jahr,
Woche, Systemzustand (nur Admins) – nur aus sichtbaren Beständen. Alle Daten erfunden."""
from datetime import date

from app import today as todaymod
from app.auth import User
from app.db import connect

TODAY = date(2026, 11, 10)


def _set(i, **f):
    with connect() as con:
        con.execute(f"UPDATE documents SET {''.join(k + '=?, ' for k in f)}status='done' WHERE id=?", (*f.values(), i))


def _doc(api, title, **f):
    i = api.upload(f"{title} Text", title).json()["id"]
    _set(i, **f)
    return i


def _build(api, is_admin):
    with connect() as con:
        return todaymod.build(con, User(api.id, "x", "X", is_admin, False), TODAY)


def test_extras_cards(admin, sam):
    for y in (2023, 2024, 2025):    # Jahresvertrag, „bis 30.11. kündbar“ – fortgeschrieben auf 2026
        _doc(admin, f"Beitrag {y + 1}", sender="Lindwurm", amount=650.0, document_date=f"{y}-11-18", direction="out",
             recurring="jährlich", due_date=f"{y}-11-30", due_note="Kündigungsfrist", due_done=1)
    _doc(admin, "Schornsteinfeger", sender="Kehrbezirk", amount=120.0, document_date="2026-05-01", tax_35a=80.0)
    _doc(admin, "Spende", sender="Tierheim", amount=50.0, document_date="2026-03-01", doc_kind="spende")
    _doc(admin, "Eingeworfen", ext_pending=1, ext_from="Oma")
    _doc(admin, "Brief vom Vorjahr", sender="Stadtwerke", document_date="2025-11-15")
    err = _doc(admin, "Fehler-Scan")
    with connect() as con:
        con.execute("UPDATE documents SET status='error' WHERE id=?", (err,))
    x = _build(admin, True)
    c = x["cancel"][0]
    assert (c["sender"], c["date"], c["per_year"], c["in_days"]) == ("Lindwurm", "2026-11-30", 650.0, 20)
    assert x["tax"] == {"year": 2026, "craft_count": 1, "craft_labor": 80.0, "donation_count": 1, "donations": 50.0}
    assert x["inbox"]["count"] == 1 and x["inbox"]["from"] == ["Oma"]
    assert [d["title"] for d in x["year_ago"]] == ["Brief vom Vorjahr", "Beitrag 2026"]   # 15.11. und 18.11.2025
    assert x["week"]["new"] >= 7
    assert {"kind": "errors", "count": 1} in x["system"]
    # Sam sieht Alex' Bestand nicht – und keinen Systemzustand
    s = _build(sam, False)
    assert s["cancel"] == [] and s["tax"] is None and s["inbox"] is None and s["year_ago"] == [] and s["system"] == []


def test_tax_year_switches_in_august():
    assert todaymod.tax_year(date(2026, 7, 31)) == 2025 and todaymod.tax_year(date(2026, 8, 1)) == 2026


def test_endpoint(admin, client):
    r = admin.get("/today/extras")
    assert r.status_code == 200 and set(r.json()) == {"cancel", "tax", "inbox", "year_ago", "week", "system"}
    assert client.get("/today/extras").status_code == 401
