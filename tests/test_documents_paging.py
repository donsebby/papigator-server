"""Seiten in den Kacheln: Gesamtzahl im Header, offset blättert."""


def test_total_count_and_offset(admin):
    ids = [admin.upload(f"Blatt {i}", f"Blatt {i}").json()["id"] for i in range(5)]
    r = admin.get("/documents", params={"limit": 2, "offset": 0, "with_total": True, "sort": "added", "dir": "asc"})
    assert r.headers["X-Total-Count"] == "5" and [d["id"] for d in r.json()] == ids[:2]
    r = admin.get("/documents", params={"limit": 2, "offset": 4, "with_total": True, "sort": "added", "dir": "asc"})
    assert [d["id"] for d in r.json()] == ids[4:]
    r = admin.get("/documents", params={"q": "Blatt", "limit": 1, "with_total": True})
    assert r.headers["X-Total-Count"] == "5" and len(r.json()) == 1
    assert "X-Total-Count" not in admin.get("/documents").headers
