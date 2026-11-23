"""Anmeldung, Sitzungen, Passwörter, Bootstrap-Token."""
from conftest import PASSWORD, login


def test_ping_public_health_needs_login(client, admin):
    assert client.get("/ping").status_code == 200
    assert client.get("/health").status_code == 401
    assert admin.get("/health").status_code == 200


def test_bootstrap_token_only_until_first_user(client, admin):
    r = client.get("/auth/me", headers={"Authorization": "Bearer boot-token"})
    assert r.status_code == 401


def test_first_user_is_admin(admin):
    assert admin.user["is_admin"] is True


def test_login_wrong_password_and_lockout(client, admin):
    for _ in range(5):
        r = client.post("/auth/login", json={"username": "alex", "password": "falsch"})
        assert r.status_code == 401
    # Danach gesperrt – auch mit richtigem Passwort
    r = client.post("/auth/login", json={"username": "alex", "password": PASSWORD})
    assert r.status_code == 429


def test_lockout_not_bypassed_by_forwarded_for(client, admin):
    """Ein gefälschter X-Forwarded-For-Header darf die Sperre pro Nutzer nicht umgehen."""
    for i in range(5):
        client.post("/auth/login", json={"username": "alex", "password": "falsch"},
                    headers={"X-Forwarded-For": f"10.0.0.{i}"})
    r = client.post("/auth/login", json={"username": "alex", "password": PASSWORD},
                    headers={"X-Forwarded-For": "10.0.0.99"})
    assert r.status_code == 429


def test_invalid_token(client, admin):
    assert client.get("/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/documents").status_code == 401


def test_logout_ends_bearer_session(client, admin):
    assert admin.post("/auth/logout").status_code == 200
    assert admin.get("/auth/me").status_code == 401


def test_user_password_change_needs_current_password(client, sam):
    r = sam.patch("/auth/me", json={"password": "neues-pass-1"})
    assert r.status_code == 403
    r = sam.patch("/auth/me", json={"password": "neues-pass-1", "current_password": "falsch"})
    assert r.status_code == 403
    r = sam.patch("/auth/me", json={"password": "kurz", "current_password": PASSWORD})
    assert r.status_code == 400
    r = sam.patch("/auth/me", json={"password": "neues-pass-1", "current_password": PASSWORD})
    assert r.status_code == 200
    login(client, "sam", "neues-pass-1")


def test_password_change_signs_out_other_devices(client, sam):
    other = login(client, "sam")
    r = sam.patch("/auth/me", json={"password": "neues-pass-1", "current_password": PASSWORD})
    assert r.status_code == 200
    assert sam.get("/auth/me").status_code == 200       # dieses Gerät bleibt angemeldet
    assert other.get("/auth/me").status_code == 401      # das andere fliegt raus


def test_admin_resets_password_without_old_one(client, admin, sam):
    r = admin.patch(f"/users/{sam.id}", json={"password": "zurueck-gesetzt"})
    assert r.status_code == 200
    assert sam.get("/auth/me").status_code == 401
    login(client, "sam", "zurueck-gesetzt")


def test_non_admin_cannot_manage_users(sam, admin):
    assert sam.post("/users", json={"username": "x", "password": PASSWORD}).status_code == 403
    assert sam.patch(f"/users/{admin.id}", json={"password": "uebernommen1"}).status_code == 403
    assert sam.delete(f"/users/{admin.id}").status_code == 403


def test_admin_cannot_demote_or_delete_self(admin):
    assert admin.patch(f"/users/{admin.id}", json={"is_admin": False}).status_code == 400
    assert admin.delete(f"/users/{admin.id}").status_code == 400


def test_username_validation(admin):
    assert admin.post("/users", json={"username": "../x", "password": PASSWORD}).status_code == 400
    assert admin.post("/users", json={"username": ".hidden", "password": PASSWORD}).status_code == 400
    assert admin.post("/users", json={"username": "ALEX", "password": PASSWORD}).status_code == 409


def test_revoke_other_sessions(client, sam):
    other = login(client, "sam")
    assert sam.delete("/auth/sessions").status_code == 204
    assert other.get("/auth/me").status_code == 401
    assert sam.get("/auth/me").status_code == 200
