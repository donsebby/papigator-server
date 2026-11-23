"""Suche in ganzen Sätzen (/search): Satz → Filter, Lockern ohne Treffer, Sichtbarkeit von Tags und Personen.
Die Fälle in search_cases.json sind dieselben Sätze wie im KI-Test (später auch für eine Swift-Fassung)."""
import datetime as dt
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config, smartsearch
from app.db import connect

CASES = json.loads((Path(__file__).parent / "search_cases.json").read_text())
TODAY = dt.date(2026, 11, 16)


@pytest.fixture
def family(admin, sam):
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})
    sam.put("/shares", json={"user_id": admin.id, "can_edit": True})
    for name in ("Gehalt", "Rechnung", "Versicherung", "Steuer"):
        admin.post("/tags", json={"name": name})
    return admin, sam


@pytest.mark.parametrize("case", CASES, ids=[c["q"] for c in CASES])
def test_sentence_becomes_filters(family, case):
    admin, _ = family
    with connect() as con:
        r = smartsearch.interpret(con, SimpleNamespace(id=admin.id), case["q"], TODAY)
    p = r["params"]
    rng = [p["date_from"], p["date_to"]] if "date_from" in p else None
    assert rng == case["range"]
    assert bool(p.get("due")) == case["due"]
    owner = next((c["label"] for c in r["chips"] if c["kind"] == "owner"), None)
    assert owner == case["owner"]
    assert [c["value"] for c in r["chips"] if c["kind"] == "word"] == case["words"]


def _dated(api, title, day, tags=""):
    d = api.upload(title, title, tags=tags).json()
    api.patch(f"/documents/{d['id']}", json={"document_date": day})
    return d["id"]


def test_search_finds_and_relaxes(family):
    admin, sam = family
    recent = _dated(admin, "Gehaltsabrechnung Oktober", (dt.date.today() - dt.timedelta(days=30)).isoformat(), "Gehalt")
    old = _dated(admin, "Gehaltsabrechnung alt", "2020-01-31", "Gehalt")
    r = admin.get("/search", params={"q": "Gehalt letzte 6 Monate"}).json()
    assert [d["id"] for d in r["documents"]] == [recent] and r["dropped"] == []
    assert {c["kind"] for c in r["chips"]} == {"word", "range"}
    # Nichts im Zeitraum: Zeitraum wird gelockert und genannt, die Suchwörter bleiben
    r = admin.get("/search", params={"q": "Gehalt letzte 1 Woche"}).json()
    assert {d["id"] for d in r["documents"]} == {recent, old} and len(r["dropped"]) == 1
    r2 = admin.get("/search", params={"q": "Gehalt von Sam"}).json()
    assert {d["id"] for d in r2["documents"]} == {recent, old} and r2["dropped"] == ["sam"]
    # Ohne Suchwörter und ohne Treffer wird nicht zu „alles“ gelockert
    r3 = admin.get("/search", params={"q": "Was ist bald fällig?"}).json()
    assert r3["documents"] == [] and [c["kind"] for c in r3["chips"]] == ["due"] and r3["dropped"] == []


def test_search_in_english(family):
    admin, sam = family
    recent = _dated(admin, "Electricity bill October", (dt.date.today() - dt.timedelta(days=30)).isoformat())
    _dated(admin, "Electricity bill old", "2020-01-31")
    sams = _dated(sam, "Electricity bill Sam", (dt.date.today() - dt.timedelta(days=10)).isoformat())
    r = admin.get("/search", params={"q": "Show me my electricity bills from the last 6 months"}).json()
    assert {d["id"] for d in r["documents"]} == {recent, sams} and r["dropped"] == []
    assert [c["value"] for c in r["chips"] if c["kind"] == "word"] == ["electricity", "bill"]
    r = admin.get("/search", params={"q": "Sam's electricity bills"}).json()
    assert [d["id"] for d in r["documents"]] == [sams]
    assert [c["kind"] for c in r["chips"]] == ["word", "word", "owner"]


def test_owner_and_date_filters_on_documents(family):
    admin, sam = family
    a = _dated(admin, "Alex Brief", "2025-03-01")
    s = _dated(sam, "Sam Brief", "2025-06-01")
    ids = lambda **p: {d["id"] for d in admin.get("/documents", params={"scope": "all", **p}).json()}
    assert ids(owner=sam.id) == {s}
    assert ids(owner=f"{admin.id},{sam.id}") == {a, s}
    assert admin.get("/documents", params={"owner": "2;drop"}).status_code == 422
    # „von Alex und Sam“: ein Chip für beide, nicht still nur der Erste
    r = admin.get("/search", params={"q": "Briefe von alex und sam"}).json()
    own = [c for c in r["chips"] if c["kind"] == "owner"]
    assert len(own) == 1 and set(own[0]["owner_ids"]) == {admin.id, sam.id} and own[0]["owner_id"] is None
    assert {d["id"] for d in r["documents"]} == {a, s}
    assert ids(date_from="2025-05-01", date_to="2025-12-31") == {s}
    assert admin.get("/documents", params={"date_from": "01.05.2025"}).status_code == 400


def test_search_reveals_no_foreign_tags_or_people(admin, make_user):
    kim = make_user("kim")
    admin.upload("Befund", "Befund Psychotherapie", tags="Psychotherapie")
    r = kim.get("/search", params={"q": "Psychotherapie von alex"}).json()
    assert r["documents"] == []
    assert all(c["kind"] == "word" and not c["tag"] for c in r["chips"])       # weder Ordner noch Person erkannt
    # Ein fremder Besitzer per Parameter liefert nichts
    assert kim.get("/documents", params={"owner": admin.id, "scope": "all"}).json() == []
