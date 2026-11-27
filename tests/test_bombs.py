"""Speicher-Angriffe (27.11.2026): eine kleine erste TIFF-Seite darf keine große zweite verstecken, und eine Mail
ohne Anhang wird nicht zu tausenden Seitenbildern."""
import pytest
from PIL import Image

from app import inbox, mail


def test_tiff_counts_every_frame(tmp_path):
    p = tmp_path / "x.tif"
    Image.new("1", (10, 10)).save(p, save_all=True, append_images=[Image.new("1", (40, 40))])
    with pytest.raises(inbox.TooManyPixels):
        inbox.images_to_pdf([p], max_pixels=1000)          # 100 + 1600 Pixel – vorher: 100 × 2 Seiten = 200
    out = inbox.images_to_pdf([p], max_pixels=5000)
    assert out.exists() and out.stat().st_size > 0


def test_mail_body_pdf_is_capped():
    out = mail.text_to_pdf("Betreff", [], "x\n" * 50_000)
    from pypdf import PdfReader
    assert len(PdfReader(out).pages) <= 40
