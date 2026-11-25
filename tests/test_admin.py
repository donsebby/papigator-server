"""Nur-Admin-Bereiche: Tags verwalten, Absender, Regeln, Einstellungen, Sicherung, Mail."""
import pytest

ADMIN_ONLY = [
    ("patch", "/tags/1", {"name": "x"}),
    ("delete", "/tags/1", None),
    ("post", "/tags/merge", {"source_ids": [1], "target_id": 2}),
    ("post", "/tags/suggest-merges", None),
    ("post", "/senders/merge", {"sources": ["a"], "target": "b"}),
    ("post", "/senders/suggest-merges", None),
    ("post", "/rules", {"pattern": "Telekom", "tag": "Telefon"}),
    ("delete", "/rules/1", None),
    ("post", "/rules/apply", None),
    ("get", "/rules", None),
    ("get", "/rules/preview?pattern=Telekom", None),
    ("patch", "/settings", {"llm_url": "http://evil"}),
    ("get", "/settings/backup", None),
    ("post", "/settings/backup/run", None),
    ("post", "/settings/backup/restore", {"name": "x", "confirm": "WIEDERHERSTELLEN"}),
    ("get", "/settings/mail", None),
    ("patch", "/settings/mail", {"mail_host": "evil"}),
    ("post", "/settings/mail/test", {}),
    ("post", "/settings/mail/check", None),
    ("post", "/settings/llm/test", {}),
    ("post", "/settings/llm/retag", None),
    ("post", "/settings/llm/pause", {"until": "forever"}),
    ("put", "/cleanup/rules", {"tag": "Rechnungen", "years": 1}),
    ("delete", "/cleanup/rules/1", None),
    ("post", "/invites", {"display_name": "Kim"}),
    ("get", "/invites", None),
    ("get", "/updates", None),
    ("delete", "/invites/1", None),
]


@pytest.mark.parametrize("method,url,body", ADMIN_ONLY)
def test_admin_only_endpoints(sam, method, url, body):
    kw = {"json": body} if body is not None else {}
    r = getattr(sam, method)(url, **kw)
    assert r.status_code == 403, (method, url, r.status_code, r.text)


def test_everyone_may_create_and_assign_tags(admin, sam):
    assert sam.post("/tags", json={"name": "Garten"}).status_code == 201
    doc = sam.upload("Rasenmäher", "Rasen").json()
    r = sam.patch(f"/documents/{doc['id']}", json={"tags": ["Garten"]})
    assert [t["name"] for t in r.json()["tags"]] == ["Garten"]


def test_tag_color_is_sanitized(admin):
    r = admin.post("/tags", json={"name": "Böse", "color": "red;background:url(x)"})
    assert r.json()["color"] is None
    tid = r.json()["id"]
    r = admin.patch(f"/tags/{tid}", json={"name": "Böse", "color": "#AABBCC"})
    assert r.json()["color"] == "#AABBCC"


def test_tag_rename_clash_and_merge(admin):
    a = admin.post("/tags", json={"name": "Rechnung"}).json()
    b = admin.post("/tags", json={"name": "Rechnungen"}).json()
    assert admin.patch(f"/tags/{b['id']}", json={"name": "rechnung"}).status_code == 409
    doc = admin.upload("x", "x", tags="Rechnungen").json()
    r = admin.post("/tags/merge", json={"source_ids": [b["id"]], "target_id": a["id"]})
    assert r.json()["moved"] == 1
    assert [t["name"] for t in admin.get(f"/documents/{doc['id']}").json()["tags"]] == ["Rechnung"]


def test_tag_counts_only_visible_documents(admin, sam):
    sam.upload("geheim", "Geheim", tags="Privat")
    tags = {t["name"]: t["count"] for t in admin.get("/tags").json()}
    assert "Privat" not in tags   # Tag hängt nur an Sams Dokument: Name bleibt verborgen


def test_rules_minimum_length(admin):
    r = admin.post("/rules", json={"pattern": "ab", "tag": "x"})
    assert r.status_code in (400, 422)


def test_settings_patch_bounds(admin):
    r = admin.patch("/settings", json={"trash_days": 0, "thumb_width": 123, "duplicate_mode": "egal"})
    s = r.json()
    assert s["trash_days"] == 1 and s["thumb_width"] == 400 and s["duplicate_mode"] == "reject"


def test_restore_rejects_bad_names(admin):
    for name in ("../etc", ".", "a/b"):
        r = admin.post("/settings/backup/restore", json={"name": name, "confirm": "WIEDERHERSTELLEN"})
        assert r.status_code == 400
    r = admin.post("/settings/backup/restore", json={"name": "x"})
    assert r.status_code == 400


def test_senders_merge_only_in_own_pots(admin, sam, make_user):
    admin.patch(f"/users/{sam.id}", json={"is_admin": True})
    d1 = admin.upload("a", "A").json()
    admin.patch(f"/documents/{d1['id']}", json={"sender": "Telekom AG"})
    d2 = sam.upload("b", "B").json()
    sam.patch(f"/documents/{d2['id']}", json={"sender": "Telekom AG"})
    r = admin.post("/senders/merge", json={"sources": ["Telekom AG"], "target": "Telekom"})
    assert r.json()["moved"] == 1
    assert sam.get(f"/documents/{d2['id']}").json()["sender"] == "Telekom AG"
