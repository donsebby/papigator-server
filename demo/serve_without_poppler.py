"""Lokale Testinstanz ohne Poppler (z. B. auf dem Mac): Vorschaubild als schlichtes Platzhalterbild, sonst
unverändert (mit `pip install pypdfium2` echte Vorschaubilder). Aufruf aus dem Server-Ordner:  PAPIGATOR_DATA=… PAPIGATOR_TOKEN=… python demo/serve_without_poppler.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image
from app import processing

def _thumb(pdf, thumb):
    """Mit pypdfium2 (falls installiert) echte Vorschau der ersten Seite – für Screenshots –, sonst ein Platzhalter."""
    thumb.parent.mkdir(parents=True, exist_ok=True)
    try:
        import pypdfium2
        page = pypdfium2.PdfDocument(str(pdf))[0]
        width = processing.config.llm_config()["thumb_width"]
        img = page.render(scale=width / page.get_width()).to_pil().convert("RGB")
        img.save(thumb, "JPEG", quality=85)
    except ImportError:
        Image.new("RGB", (300, 424), (240, 240, 235)).save(thumb, "JPEG")

processing.make_thumbnail = _thumb
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
