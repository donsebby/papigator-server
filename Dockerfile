# Papigator server image
#
#   docker build -t papigator-server .
#
# One image, two processes (see docker-compose.yml):
#   - main app (web app, API, OCR worker, inbox, mail import, backups) on port 8000
#   - public share pages (/s/… only) on port 8001:  command: uvicorn app.share_app:app --port 8001
#
# Data lives in /data, backups in /backup. The container starts as root only long enough to make
# sure both folders belong to the service user (PUID/PGID, default 1000), then drops privileges.

# ---------- Python dependencies ----------
FROM python:3.12-slim-trixie AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# Current pip: older resolvers (25.0) fail on ocrmypdf's pikepdf[pdfa] requirement
RUN python -m venv /venv && /venv/bin/pip install --upgrade pip
COPY requirements.txt /tmp/requirements.txt
RUN /venv/bin/pip install -r /tmp/requirements.txt


# ---------- Runtime ----------
FROM python:3.12-slim-trixie

LABEL org.opencontainers.image.title="Papigator server" \
      org.opencontainers.image.description="Self-hosted document archive: OCR, full-text search, optional local AI tagging" \
      org.opencontainers.image.licenses="AGPL-3.0-only"

# OCR toolchain used by ocrmypdf (same packages as a bare-metal install):
# tesseract with German + English (+ OSD for page rotation), ghostscript, qpdf, unpaper, pngquant,
# poppler-utils (pdftoppm for thumbnails). Add more tesseract-ocr-<lang> packages here if needed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng tesseract-ocr-osd \
      ghostscript qpdf unpaper pngquant poppler-utils \
      tini tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1000 papigator \
 && useradd --system --uid 1000 --gid papigator --home-dir /data --no-create-home --shell /usr/sbin/nologin papigator \
 && mkdir -p /data /backup \
 && chown papigator:papigator /data /backup

COPY --from=build /venv /venv
ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PAPIGATOR_DATA=/data \
    PAPIGATOR_BACKUP=/backup

WORKDIR /app
COPY app ./app
COPY THIRD_PARTY.md LICENSE NOTICE ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/ping', timeout=4); sys.exit(0)" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
