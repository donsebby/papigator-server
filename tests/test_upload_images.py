"""Fotos hochladen: JPEG/PNG/TIFF werden zur PDF (mehrere Bilder = mehrseitig), HEIC wird klar abgelehnt."""
import io

from PIL import Image
from pypdf import PdfReader

from app import config, processing
from conftest import make_pdf


def _img(fmt: str, color="red", size=(60, 40), **kw) -> bytes:
    buf = io.BytesIO()
    mode = "RGBA" if fmt == "PNG" and kw.pop("alpha", False) else "RGB"
    Image.new(mode, size, color).save(buf, fmt, **kw)
    return buf.getvalue()


def _pages(doc_id: int) -> list:
    return PdfReader(str(processing.doc_path(doc_id))).pages


def test_jpeg_becomes_pdf(admin):
    r = admin.post("/documents", files={"file": ("IMG_0001.jpg", _img("JPEG"), "image/jpeg")}, data={"title": "Foto"})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["title"] == "Foto" and d["original_name"] == "IMG_0001.jpg"
    assert processing.doc_path(d["id"]).read_bytes()[:5] == b"%PDF-"
    assert len(_pages(d["id"])) == 1


def test_png_with_transparency_and_without_title(admin):
    r = admin.post("/documents", files={"file": ("scan.png", _img("PNG", (0, 0, 0, 0), alpha=True), "image/png")})
    assert r.status_code == 201, r.text
    assert r.json()["title"] == "scan"


def test_detected_by_content_not_name(admin):
    """Ein JPEG mit falschem Namen und Typ wird trotzdem erkannt."""
    r = admin.post("/documents", files={"file": ("upload", _img("JPEG", "blue"), "application/octet-stream")})
    assert r.status_code == 201, r.text


def test_several_images_one_pdf(admin):
    files = [("file", (f"seite{i}.jpg", _img("JPEG", c), "image/jpeg")) for i, c in enumerate(["red", "green", "blue"])]
    files.append(("file", ("seite4.png", _img("PNG", "white"), "image/png")))
    r = admin.post("/documents", files=files, data={"title": "Vier Seiten"})
    assert r.status_code == 201, r.text
    assert len(_pages(r.json()["id"])) == 4


def test_multipage_tiff(admin):
    buf = io.BytesIO()
    a, b = Image.new("RGB", (30, 30), "red"), Image.new("RGB", (30, 30), "blue")
    a.save(buf, "TIFF", save_all=True, append_images=[b])
    r = admin.post("/documents", files={"file": ("fax.tif", buf.getvalue(), "image/tiff")})
    assert r.status_code == 201, r.text
    assert len(_pages(r.json()["id"])) == 2


def test_exif_rotation_applied(admin):
    """Handyfotos tragen die Drehung nur in EXIF – die Seite soll aufrecht sein (hier: hochkant)."""
    im = Image.new("RGB", (80, 40), "red")
    exif = im.getexif()
    exif[0x0112] = 6                                   # „um 90° drehen“
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes())
    r = admin.post("/documents", files={"file": ("hoch.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 201, r.text
    box = _pages(r.json()["id"])[0].mediabox
    assert float(box.height) > float(box.width)


def test_heic_rejected_with_clear_message(admin):
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
    r = admin.post("/documents", files={"file": ("IMG_0002.HEIC", heic, "image/heic")})
    assert r.status_code == 415
    assert r.json()["detail"] == "HEIC wird nicht unterstützt – als JPEG senden"


def test_unknown_and_broken_files(admin):
    r = admin.post("/documents", files={"file": ("notiz.txt", b"nur Text", "text/plain")})
    assert r.status_code == 415
    broken = _img("JPEG")[:40]                       # Anfang stimmt, Rest fehlt
    r = admin.post("/documents", files={"file": ("kaputt.jpg", broken, "image/jpeg")})
    assert r.status_code == 422 and r.json()["detail"] == "Bild lässt sich nicht lesen"


def test_pdf_still_works_and_no_pdf_mix(admin):
    assert admin.upload(title="Normal").status_code == 201
    files = [("file", ("a.pdf", make_pdf("a"), "application/pdf")), ("file", ("b.jpg", _img("JPEG"), "image/jpeg"))]
    assert admin.post("/documents", files=files).status_code == 415


def test_same_photo_twice_is_duplicate(admin):
    img = _img("JPEG", "purple")
    assert admin.post("/documents", files={"file": ("a.jpg", img, "image/jpeg")}).status_code == 201
    assert admin.post("/documents", files={"file": ("a.jpg", img, "image/jpeg")}).status_code == 409


def test_demo_limit_applies_to_images(admin, monkeypatch):
    monkeypatch.setattr(config, "DEMO", True)
    monkeypatch.setattr(config, "DEMO_MAX_UPLOAD_MB", 0)
    r = admin.post("/documents", files={"file": ("a.jpg", _img("JPEG"), "image/jpeg")})
    assert r.status_code == 413


def test_pixel_budget(admin, monkeypatch):
    from app import inbox
    monkeypatch.setattr(inbox, "MAX_TOTAL_PIXELS", 5000)
    files = [("file", (f"s{i}.jpg", _img("JPEG", c), "image/jpeg")) for i, c in enumerate(["red", "green", "blue"])]
    r = admin.post("/documents", files=files)
    assert r.status_code == 413, r.text


def test_no_file_descriptor_leak(admin):
    import os
    before = len(os.listdir("/dev/fd"))
    for i in range(30):
        assert admin.post("/documents", files={"file": (f"f{i}.png", _img("PNG", (i * 8, 255 - i * 8, 0)), "image/png")}).status_code == 201
    assert len(os.listdir("/dev/fd")) - before < 5
