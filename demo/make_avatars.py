"""Erfundene Profilbilder für die Demo (Alex, Sam, Mia): einfache gezeichnete Gesichter auf farbigem Grund – keine
Fotos echter Menschen. Pillow genügt; seed.py lädt sie über /auth/me/avatar bzw. /persons/<id>/avatar hoch.

    python demo/make_avatars.py /tmp/avatare   # zum Ansehen als PNG speichern
"""
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw

# Hintergrund, Haut, Haare, Frisur
PEOPLE = {
    "alex": ("#7dd3fc", "#f1c7a3", "#5b3a29", "short"),
    "sam": ("#f9a8d4", "#e0ac83", "#1f2937", "long"),
    "mia": ("#fcd34d", "#f6d3b5", "#b45309", "pigtails"),
}


def face(bg: str, skin: str, hair: str, style: str, size: int = 512) -> bytes:
    s = size / 512
    im = Image.new("RGB", (size, size), bg)
    d = ImageDraw.Draw(im)
    box = lambda x0, y0, x1, y1: [x0 * s, y0 * s, x1 * s, y1 * s]
    if style == "long":                                   # Haare hinter dem Kopf
        d.rounded_rectangle(box(120, 150, 392, 470), radius=110 * s, fill=hair)
    if style == "pigtails":
        d.ellipse(box(78, 220, 170, 330), fill=hair)
        d.ellipse(box(342, 220, 434, 330), fill=hair)
    d.rounded_rectangle(box(196, 360, 316, 470), radius=30 * s, fill=skin)          # Hals
    d.ellipse(box(110, 470 - 90, 402, 470 + 180), fill="#ffffff")                    # Schultern/Shirt
    d.ellipse(box(146, 120, 366, 380), fill=skin)                                    # Kopf
    if style == "short":
        d.chord(box(140, 100, 372, 300), 180, 360, fill=hair)
        d.rectangle(box(140, 196, 160, 250), fill=hair)
        d.rectangle(box(352, 196, 372, 250), fill=hair)
    else:
        d.chord(box(138, 96, 374, 320), 180, 360, fill=hair)
    for x in (212, 300):                                                              # Augen
        d.ellipse(box(x - 13, 238, x + 13, 266), fill="#1f2937")
        d.ellipse(box(x - 4, 243, x + 4, 251), fill="#ffffff")
    d.ellipse(box(190, 282, 226, 300), fill="#f9a8a8")                                # Wangen
    d.ellipse(box(286, 282, 322, 300), fill="#f9a8a8")
    d.arc(box(222, 278, 290, 330), 20, 160, fill="#7c2d12", width=int(7 * s))         # Lächeln
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def avatar(name: str) -> bytes:
    return face(*PEOPLE[name])


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    target.mkdir(parents=True, exist_ok=True)
    for n in PEOPLE:
        (target / f"{n}.png").write_bytes(avatar(n))
