# Danke – Open-Source-Komponenten in Papigator

Papigator steht auf den Schultern dieser Projekte. Sie werden unverändert als eigenständige
Programme oder Bibliotheken genutzt; die jeweiligen Lizenztexte liegen den Paketen bei
(Debian: `/usr/share/doc/<paket>/copyright`, Python: `pip show -f <paket>` bzw. `site-packages/*.dist-info/`).

## Texterkennung und PDF

| Projekt | Wofür | Lizenz | Quelle |
|---|---|---|---|
| Tesseract OCR | Texterkennung gescannter Seiten | Apache 2.0 | https://github.com/tesseract-ocr/tesseract |
| OCRmyPDF | OCR-Pipeline, durchsuchbare PDF/A | MPL 2.0 | https://github.com/ocrmypdf/OCRmyPDF |
| Ghostscript | PDF-Verarbeitung innerhalb von OCRmyPDF (als eigenständiges Programm aufgerufen, unverändert) | AGPL 3.0 | https://www.ghostscript.com |
| qpdf | PDF-Strukturen prüfen und reparieren | Apache 2.0 | https://github.com/qpdf/qpdf |
| Poppler (`pdftoppm`) | Vorschaubilder der ersten Seite | GPL 2.0 | https://poppler.freedesktop.org |
| unpaper | Scans begradigen und säubern | GPL 2.0 | https://github.com/unpaper/unpaper |
| pngquant | Bilder in OCR-PDFs verkleinern | GPL 3.0 / BSD-ähnlich | https://pngquant.org |
| pypdf | PDFs lesen, zusammenführen, Seiten zählen | BSD 3-Clause | https://github.com/py-pdf/pypdf |
| Pillow | Bildverarbeitung | MIT-CMU | https://python-pillow.org |

## Server

| Projekt | Wofür | Lizenz | Quelle |
|---|---|---|---|
| FastAPI | HTTP-API | MIT | https://fastapi.tiangolo.com |
| Uvicorn | ASGI-Server | BSD 3-Clause | https://www.uvicorn.org |
| Starlette | Grundlage von FastAPI | BSD 3-Clause | https://www.starlette.io |
| Pydantic | Datenmodelle | MIT | https://docs.pydantic.dev |
| httpx | HTTP-Client (LLM-Anbindung) | BSD 3-Clause | https://www.python-httpx.org |
| python-multipart | Datei-Uploads | Apache 2.0 | https://github.com/Kludex/python-multipart |
| bcrypt | Passwort-Hashing | Apache 2.0 | https://github.com/pyca/bcrypt |
| cryptography | Verschlüsselung gespeicherter Geheimnisse (Fernet) | Apache 2.0 / BSD 3-Clause | https://github.com/pyca/cryptography |
| SQLite mit FTS5 | Datenbank und Volltextsuche | Public Domain | https://sqlite.org |
| Python | Laufzeit | PSF License | https://www.python.org |

## Docker-Image

Das Image (`Dockerfile`) baut auf dem offiziellen Python-Image auf und installiert die oben genannten
OCR-Programme aus den Debian-Paketquellen, dazu:

| Projekt | Wofür | Lizenz | Quelle |
|---|---|---|---|
| Debian GNU/Linux (`python:3.12-slim-trixie`) | Basis-System des Containers | verschiedene freie Lizenzen (je Paket) | https://www.debian.org |
| tini | Init-Prozess im Container (Signale, Zombie-Prozesse) | MIT | https://github.com/krallin/tini |
| util-linux (`setpriv`) | Rechte abgeben nach dem Start | GPL 2.0+ | https://github.com/util-linux/util-linux |
| tzdata | Zeitzonen | Public Domain | https://www.iana.org/time-zones |

## Weboberfläche

| Projekt | Wofür | Lizenz | Quelle |
|---|---|---|---|
| QR Code generator (Project Nayuki) | Vorbild für `app/static/qr.js` (QR-Code der Einladungslinks): eigene, gekürzte Umsetzung nach ISO/IEC 18004 – Byte-Modus, Stufe M, Version 1–10 –, Aufbau und Rechenwege nach diesem Projekt | MIT | https://www.nayuki.io/page/qr-code-generator-library |

## KI (optional, läuft außerhalb des Servers)

| Projekt | Wofür | Lizenz | Quelle |
|---|---|---|---|
| Ollama | Lokaler LLM-Server | MIT | https://ollama.com |
| Gemma | Sprachmodell für Tags, Zusammenfassungen und Fragen | Gemma Terms of Use | https://ai.google.dev/gemma |

## iPhone-App

Die App nutzt ausschließlich Apple-Frameworks (VisionKit, Vision, PDFKit, SwiftUI, Core Spotlight,
App Intents, Core Image für QR-Codes) und bündelt keinen Fremdcode.
