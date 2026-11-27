"""Profilbilder: eigenes Bild und Bilder für Personen ohne Konto – verkleinert, ohne Metadaten, nur für Bekannte
sichtbar, nur vom Anlegenden änderbar, mit dem Konto bzw. der Person wieder weg, in der Sicherung."""
import io

from PIL import Image

from app import avatars, config


def _photo(w=900, h=600, color=(200, 80, 40), exif=True) -> bytes:
    im = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    if exif:
        ex = Image.Exif()
        ex[0x010F] = "Testkamera"            # Make
        ex[0x8825] = {2: (52.0, 31.0, 0.0)}  # GPS-Breite
        im.save(buf, "JPEG", exif=ex.tobytes())
    else:
        im.save(buf, "PNG")
    return buf.getvalue()


def _up(api, url, data, name="bild.jpg"):
    return api.put(url, files={"file": (name, data, "image/jpeg")})


def test_own_avatar_small_square_without_metadata(admin):
    assert admin.get("/auth/me").json()["avatar"] is None
    r = _up(admin, "/auth/me/avatar", _photo())
    assert r.status_code == 200 and r.json()["avatar"]
    me = admin.get("/auth/me").json()
    assert me["avatar"] == r.json()["avatar"]
    img = admin.get(f"/users/{me['id']}/avatar")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(img.content)) as im:
        assert im.size == (256, 256) and not im.getexif()      # Ort und Kamera sind weg
    # PNG geht auch; Unsinn nicht
    assert _up(admin, "/auth/me/avatar", _photo(exif=False), "b.png").status_code == 200
    assert _up(admin, "/auth/me/avatar", b"kein bild").status_code == 400
    assert admin.delete("/auth/me/avatar").status_code == 204
    assert admin.get("/auth/me").json()["avatar"] is None
    assert admin.get(f"/users/{me['id']}/avatar").status_code == 404


def test_account_avatar_only_for_people_who_know_you(admin, sam, make_user):
    jo = make_user("jo")
    _up(sam, "/auth/me/avatar", _photo())
    assert jo.get(f"/users/{sam.id}/avatar").status_code == 404           # kennt Sam nicht
    assert admin.get(f"/users/{sam.id}/avatar").status_code == 200        # Admin kennt alle
    sam.put("/shares", json={"user_id": jo.id, "can_edit": False})
    assert jo.get(f"/users/{sam.id}/avatar").status_code == 200
    assert jo.get("/auth/me").json()["shared_with_me"][0]["avatar"]
    # Konto weg → Bild weg
    admin.delete(f"/users/{sam.id}")
    assert not (config.DATA_DIR / "avatars" / f"u{sam.id}.jpg").exists()


def test_person_avatar_rights(admin, sam):
    p = admin.post("/persons", json={"name": "Mia"}).json()
    assert p["avatar"] is None
    assert _up(admin, f"/persons/{p['id']}/avatar", _photo()).status_code == 200
    assert admin.get("/persons").json()[0]["avatar"]
    assert admin.get(f"/persons/{p['id']}/avatar").status_code == 200
    # Sam sieht Alex' Bestand nicht: weder Bild noch ändern
    assert sam.get(f"/persons/{p['id']}/avatar").status_code == 404
    assert _up(sam, f"/persons/{p['id']}/avatar", _photo()).status_code == 404
    # Nur lesen: sehen ja, ändern nein
    admin.put("/shares", json={"user_id": sam.id, "can_edit": False})
    assert sam.get(f"/persons/{p['id']}/avatar").status_code == 200
    assert _up(sam, f"/persons/{p['id']}/avatar", _photo()).status_code == 403
    assert sam.delete(f"/persons/{p['id']}/avatar").status_code == 403
    # Mit Schreibrecht darf Sam es ändern
    admin.put("/shares", json={"user_id": sam.id, "can_edit": True})
    assert _up(sam, f"/persons/{p['id']}/avatar", _photo(color=(0, 0, 200))).status_code == 200
    # Person entfernt → Bild weg
    admin.delete(f"/persons/{p['id']}")
    assert avatars.version("p", p["id"]) is None


def test_avatars_are_in_the_backup(admin, tmp_path):
    from app import backup
    _up(admin, "/auth/me/avatar", _photo())
    from app.db import connect, set_setting
    with connect() as con:
        set_setting(con, "backup_dir", str(tmp_path))
    snap = backup.run()
    assert (tmp_path / snap["name"] / "avatars" / f"u{admin.id}.jpg").is_file()


def test_deleted_account_leaves_no_person_photo_or_private_tag(admin, make_user, sam):
    """Konto gelöscht (27.11.2026): seine Personen, Fotos und leeren eigenen Tags verschwinden – „Lotta“ neu angelegt
    bekommt kein fremdes Kinderfoto, „Psychotherapie“ steht danach nicht in der Tag-Liste anderer."""
    kim = make_user("kim")
    p = kim.post("/persons", json={"name": "Lotta"}).json()
    assert _up(kim, f"/persons/{p['id']}/avatar", _photo()).status_code == 200
    d = kim.upload(text="Termin", title="Sitzung").json()
    kim.patch(f"/documents/{d['id']}", json={"tags": ["Psychotherapie"]})
    kid = next(u["id"] for u in admin.get("/users").json() if u["username"] == "kim")
    assert admin.delete(f"/users/{kid}").status_code == 204
    names = {t["name"] for t in sam.get("/tags").json()}
    assert "Lotta" not in names and "Psychotherapie" not in names
    q = sam.post("/persons", json={"name": "Lotta"}).json()
    assert sam.get(f"/persons/{q['id']}/avatar").status_code == 404
