"""Eigenes Konto löschen (DELETE /auth/me) – Pflicht für die App im App Store."""
from app import config, processing
from app.db import connect
from conftest import PASSWORD


def _delete_me(api, password=PASSWORD):
    return api.client.request("DELETE", "/auth/me", json={"password": password},
                              headers={"Authorization": f"Bearer {api.token}"})


def test_delete_own_account_removes_everything(admin, make_user):
    kim = make_user("kim")
    doc = kim.upload("Rechnung von Kim", title="Kim Rechnung").json()["id"]
    csv_ = "Buchungsdatum;Zahlungsempfänger;Verwendungszweck;Betrag\n01.11.2026;A;B;-1,00\n"
    assert kim.post("/bank/import", files={"file": ("x.csv", csv_.encode(), "text/csv")},
                    data={"account_name": "Giro"}).status_code == 200
    # eine Frist von Alex, für die Kim zuständig ist, gilt danach wieder für alle
    mine = admin.upload("Alex Frist", title="Frist").json()["id"]
    assert kim.get("/auth/me").status_code == 200
    with connect() as con:
        con.execute("UPDATE documents SET due_assignee=? WHERE id=?", (kim.id, mine))
    inbox = config.INBOX_DIR / "kim"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "scan.pdf").write_bytes(b"%PDF-1.4")

    assert _delete_me(kim, "falsch").status_code == 403
    assert _delete_me(kim).status_code == 204
    assert kim.get("/auth/me").status_code == 401                          # Sitzung ist weg
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM users WHERE username='kim'").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM documents WHERE id=?", (doc,)).fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM bank_accounts").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM bank_tx").fetchone()[0] == 0
        assert con.execute("SELECT due_assignee FROM documents WHERE id=?", (mine,)).fetchone()[0] is None
    assert not processing.doc_path(doc).exists()
    assert not inbox.exists()


def test_last_admin_must_hand_over_first(admin, make_user):
    kim = make_user("kim")
    r = _delete_me(admin)
    assert r.status_code == 400 and "Admin" in r.json()["detail"]
    assert admin.patch(f"/users/{kim.id}", json={"is_admin": True}).status_code == 200
    assert _delete_me(admin).status_code == 204


def test_sole_user_may_leave_server_needs_setup_again(client, admin):
    assert _delete_me(admin).status_code == 204
    assert client.get("/ping").json()["needs_setup"] is True


def test_demo_keeps_shared_accounts_but_test_account_may_go(admin, make_user, monkeypatch):
    shared, tester = make_user("sam"), make_user("loeschtest")
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DEMO_KEEP_USERS", {"alex", "sam", "verwaltung"})
    r = _delete_me(shared)
    assert r.status_code == 403 and r.json()["detail"] == "In der Demo nicht möglich"
    assert _delete_me(tester).status_code == 204
