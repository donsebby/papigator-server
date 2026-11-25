"""Update-Meldung: neuere Version auf GitHub → Hinweis für Admins; zwischengespeichert, abschaltbar, offline still."""
from app import updates
from app.version import VERSION


def test_version_compare():
    assert updates.newer("v2026.11.26", "2026.11.25.1")
    assert updates.newer("2026.11.25.2", "2026.11.25.1")
    assert not updates.newer("2026.11.25", "2026.11.25.1")
    assert not updates.newer("2026.11.25.1", "2026.11.25.1")
    assert not updates.newer("", "2026.11.25.1") and not updates.newer(None)


def test_admin_sees_newer_release_cached_and_switchable(admin, monkeypatch):
    calls = []
    def fake():
        calls.append(1)
        return {"version": "2099.1.1", "url": "https://github.com/donsebby/papigator-server/releases/tag/v2099.1.1",
                "notes": "Neu: alles", "published": None}
    monkeypatch.setattr(updates, "_fetch", fake)
    r = admin.get("/updates").json()
    assert r["checked"] and {k: v for k, v in r.items() if k != "checked"} == {
        "current": VERSION, "enabled": True, "latest": "2099.1.1", "newer": True,
        "url": "https://github.com/donsebby/papigator-server/releases/tag/v2099.1.1", "notes": "Neu: alles"}
    admin.get("/updates")
    assert len(calls) == 1                          # zwischengespeichert, nicht bei jedem Aufruf
    admin.get("/updates", params={"force": True})
    assert len(calls) == 2                          # „Jetzt prüfen“
    calls.pop()
    assert admin.get("/health").json()["version"] == VERSION
    # Abschalten: keine Anfrage mehr, kein Hinweis
    assert admin.patch("/settings", json={"update_check": False}).status_code == 200
    assert admin.get("/settings").json()["update_check"] is False
    r = admin.get("/updates").json()
    assert r["enabled"] is False and r["newer"] is False and len(calls) == 1
    admin.patch("/settings", json={"update_check": True})
    admin.get("/updates")
    assert len(calls) == 2                          # wieder an: gleich frisch nachgesehen


def test_offline_or_no_release_is_quiet(admin, monkeypatch):
    monkeypatch.setattr(updates, "_fetch", lambda: None)
    r = admin.get("/updates").json()
    assert r["newer"] is False and r["checked"] is None             # GitHub nie erreicht
    monkeypatch.setattr(updates, "_fetch", lambda: {})
    assert admin.get("/updates").json()["latest"] is None


def test_not_for_regular_users(sam):
    assert sam.get("/updates").status_code == 403
