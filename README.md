# Papigator server

A small, self-hosted document archive for a household. Drop a PDF in (or scan it with your phone or
office scanner), and Papigator makes it searchable: OCR, thumbnails, full-text search and, if you want,
titles, senders, dates, tags and due dates suggested by an AI model running on your own hardware.

This repository contains the server (FastAPI, SQLite with FTS5, OCRmyPDF/Tesseract) and the web app it
serves. Everything stays on your machine; nothing is sent anywhere unless you configure it.

> The web app's interface is currently in German.

<!-- Screenshots: add images to docs/ and link them here, e.g. ![Overview](docs/overview.png) -->

## Features

- **Import from anywhere**: drag and drop in the web app, the iPhone/iPad app (scans with on-device OCR),
  a watched scanner inbox folder (FTP/SMB), or an IMAP mailbox (PDF and image attachments).
- **OCR** with OCRmyPDF and Tesseract (German and English included), page rotation and deskewing;
  a searchable text layer is written into the PDF.
- **Search**: SQLite FTS5 full-text search with highlighted snippets, plus whole-sentence queries like
  "insurance from Sam" or "what is due soon?" that are split into filters (period, person, open
  deadlines, keywords) without any AI.
- **Organise**: tags and folders, keyword rules, saved views, senders, merging documents, a review queue,
  a duplicate finder, trash with automatic cleanup.
- **Deadlines**: payment due dates, notice periods, warranty ends – with an overview of what is overdue
  or due soon.
- **Optional AI** (any OpenAI-compatible API, e.g. Ollama): suggests title, sender, date, tags, summary
  and deadlines, and answers questions about your documents ("Ask Papigator"). Works without it.
- **Several users**: everyone has their own documents; share your whole collection with a partner,
  read-only or editable.
- **Share links** for people outside your network – a document, a selection, a folder or a saved search,
  optional password and expiry – and **upload boxes** where others can drop files for you. Served by a separate process on its
  own port, so the actual app never needs to face the internet.
- **Built-in backups**: nightly snapshots with hardlinks, retention, restore from the web app or CLI.
- **Export** everything (or a selection) as ZIP with a `metadata.json`.

## Quick start (Docker Compose)

Requirements: a Linux host (or any machine with Docker), Docker Engine with the Compose plugin,
about 1 GB RAM plus whatever your AI model needs.

```bash
# 1. Get the source onto your host, then:
git clone https://github.com/donsebby/papigator-server.git
cd papigator-server
cp .env.example .env

# 2. Fill in the two required values in .env
openssl rand -base64 32   # → PAPIGATOR_SECRET_KEY
openssl rand -hex 24      # → PAPIGATOR_TOKEN

# 3. Start – builds the image from this folder
docker compose up -d --build
#    or, with a prebuilt image (PAPIGATOR_IMAGE set in .env), without --build:
#    docker compose pull && docker compose up -d
docker compose ps         # both services should become "healthy"
curl -s http://localhost:8000/ping
# {"ok":true,"needs_setup":true}
```

4. Open `http://<host>:8000/app`, click **Ersteinrichtung** (first-run setup), enter the
   `PAPIGATOR_TOKEN` from your `.env` and create the first admin user. From then on the bootstrap
   token no longer works; further users are created by an admin in the settings.
5. Under **Einstellungen** (settings) check *Ablage → Sicherung* (backup, `/backup` is preset) and,
   if you like, *KI-Erkennung* (AI recognition).

Keep a copy of `PAPIGATOR_SECRET_KEY` in your password manager. Without it, a restored backup cannot
decrypt the stored mail password and AI API keys, or show the addresses of existing share links
again (the links themselves keep working; everything else is unaffected).

### What runs

| Service | Port | Purpose |
|---|---|---|
| `papigator` | 8000 | Web app (`/app`), API (interactive docs at `/docs` with `PAPIGATOR_API_DOCS=1`), OCR worker, scanner inbox, mail import, backups |
| `papigator-share` | 8001 | Only the public share pages `/s/…` (share links, upload boxes) |

Both use the same image and the same `./data` folder. The main service creates and migrates the
database; the share service waits until it is healthy. If you never share outside your network,
delete the `papigator-share` service.

### Volumes and permissions

| Host | Container | Content |
|---|---|---|
| `./data` | `/data` | `papigator.db`, `documents/`, `thumbnails/`, `inbox/<username>/` |
| `./backup` | `/backup` | Nightly snapshots – put this on a NAS or a second disk |

The container starts as root only to make both folders belong to `PUID`/`PGID` (default 1000), then
runs the server as that unprivileged user. If a network share does not allow changing owners, set
`PUID`/`PGID` to an account that can write there.

## Unraid

Papigator has a template for Unraid's Docker manager in [`templates/papigator.xml`](templates/papigator.xml)
(one container: web app, API, OCR, scanner inbox, backups – no public share pages).

1. Install it from **Apps** (Community Applications) by searching for *Papigator*. Without Community
   Applications, fetch the template in the Unraid terminal and pick *Papigator* under
   *Docker → Add Container → Template*:
   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-Papigator.xml \
     https://raw.githubusercontent.com/donsebby/papigator-server/main/templates/papigator.xml
   ```
2. Fill in **Setup token** (any long random string, e.g. `openssl rand -hex 24` in the Unraid terminal).
   **Secret key** is optional but recommended (`openssl rand -base64 32`, keep a copy); leave it empty and
   Papigator creates one in the app data folder.
3. Optionally map the share your scanner writes to as **Scanner inbox**, then *Apply*.
4. Open the WebUI, click **Ersteinrichtung** (first-run setup), enter the setup token and create the
   first admin.

App data lives in `/mnt/user/appdata/papigator`, nightly backups in `/mnt/user/backups/papigator`, and the
server runs as `nobody:users` (99:100). For AI recognition point *AI server URL* (advanced view) or
*Settings → AI recognition* at an Ollama container, e.g. `http://<unraid-ip>:11434/v1`.

## Configuration

Environment variables are read at start. Values marked *starting value* can later be changed in the
web app; the setting stored in the database then wins.

| Variable | Default | Description |
|---|---|---|
| `PAPIGATOR_SECRET_KEY` | – (required in Compose) | Master key for stored secrets. Without it the server creates `/data/.papigator.key`, which backups deliberately skip. |
| `PAPIGATOR_TOKEN` | – (required in Compose) | Bootstrap token for the first-run setup; ignored once a user exists. |
| `PAPIGATOR_DATA` | `/data` | Data folder (database, PDFs, thumbnails). |
| `PAPIGATOR_BACKUP` | `/backup` in the image | Backup target folder; empty disables backups. *Starting value.* |
| `PAPIGATOR_INBOX` | `$PAPIGATOR_DATA/inbox` | Scanner inbox; files in `<inbox>/<username>/` are imported for that user. |
| `PAPIGATOR_INBOX_POLL` | `10` | Seconds between inbox scans. |
| `PAPIGATOR_OCR_LANGS` | `deu+eng` | Tesseract languages. *Starting value.* |
| `PAPIGATOR_PUBLIC_URL` | – | External address used in share and upload links, e.g. `https://papigator.example.com`. *Starting value.* |
| `PAPIGATOR_LLM_URL` | – | OpenAI-compatible API base URL, e.g. `http://ollama:11434/v1`. Empty = AI off. *Starting value.* |
| `PAPIGATOR_LLM_MODEL` | `gemma4:12b-mlx` | Model name on that server. *Starting value.* |
| `PAPIGATOR_LLM_KEY` | `none` | API key sent as Bearer token, if the server needs one. |
| `PAPIGATOR_LLM_MAX_CHARS` | `6000` | How much document text is sent to the model per document. |
| `PUID` / `PGID` | `1000` | User and group the server runs as inside the container. |
| `TZ` | `Etc/UTC` | Time zone (backup time, dates). |
| `PAPIGATOR_IMAGE`, `PAPIGATOR_PORT`, `PAPIGATOR_SHARE_PORT` | – / `8000` / `8001` | Compose only: image to use and host ports. |

## AI recognition

Papigator talks to any server with an OpenAI-compatible chat API. Recommended is
[Ollama](https://ollama.com) on your own hardware:

- **Ollama on the Docker host**: `http://host.docker.internal:11434/v1` (the compose file maps this
  name to the host). Make Ollama listen on all interfaces (`OLLAMA_HOST=0.0.0.0`).
- **Ollama in the same compose project**: uncomment the `ollama` service, then
  `docker compose exec ollama ollama pull gemma4:e4b` and use `http://ollama:11434/v1`.
- **Another machine in your network**: e.g. `http://192.0.2.10:11434/v1`. LM Studio, vLLM and similar
  work too (`…/v1`).

In the web app (*Einstellungen → KI-Erkennung*) you can set a main and a fallback server, a separate
model for questions, test the connection and pause recognition. Small models (around 4–12B
parameters) are enough for tagging.

**Cloud providers** with an OpenAI-compatible endpoint can be configured as well, but Papigator treats
every address outside your private network as *cloud* and sends no document text there until an admin
explicitly approves that host and enables it per task (recognition, questions). API keys are stored
encrypted and only sent to the host they were entered for.

## Scanner inbox and mail import

- **Scanner (FTP/SMB)**: everything that lands in `data/inbox/<username>/` is imported for that user
  (PDF, JPG, PNG, TIFF) and then removed; failed files move to `_fehler/`. Subfolders become tags
  (`inbox/alex/House/Power/scan.pdf` gets the tag "Power" in the folder "House"); files directly in
  `inbox/` go to the first admin. Papigator has no FTP server of its own – point a separate FTP/SMB
  server (container or host service) at that folder and give it the same UID/GID as `PUID`/`PGID`.
- **Mail**: *Einstellungen → E-Mail-Eingang* (admin): IMAP host, folder and interval. PDF and image
  attachments of unread mails are imported; mails without attachments can be stored as a text PDF.
  The mail password is stored encrypted with `PAPIGATOR_SECRET_KEY`.

## Backups and restore

The server writes a snapshot every night (default 03:00) to `/backup/JJJJ-MM-TT_HHMM/`: a consistent copy
of the database (SQLite backup API, safe while running), `documents/`, `thumbnails/` and a manifest.
Unchanged files are hardlinks to the previous snapshot. Retention, time and target are set in the web
app, where you can also back up and restore with one click.

From the command line (run as the service user so file ownership stays correct):

```bash
docker compose exec -u papigator papigator python -m app.backup            # status
docker compose exec -u papigator papigator python -m app.backup run        # back up now
docker compose exec -u papigator papigator python -m app.backup restore 2026-11-09_0300
```

The secret key is intentionally not part of the snapshot – keep it separately. A backup on the same
disk protects against mistakes, not against disk failure; mount a NAS folder as `/backup`
(e.g. `/mnt/nas/papigator:/backup`).

## Updating

```bash
# built from source
git pull && docker compose up -d --build
# prebuilt image (PAPIGATOR_IMAGE set)
docker compose pull && docker compose up -d
```

Database migrations run automatically when the main service starts. Take a backup first.

## Security notes

- Run Papigator **inside your home network** and reach it from outside via VPN (WireGuard, Tailscale …).
  Don't expose port 8000 to the internet.
- If you want to share documents with people outside, publish **only port 8001** (the share service)
  through a reverse proxy with HTTPS (Caddy, nginx, Traefik …), and only the path `/s/*`. Upload boxes
  accept files of up to 50 MB per request, so allow at least 51 MB request bodies on that route.
- Behind a reverse proxy, pass `X-Forwarded-Proto: https`, `X-Forwarded-For` and `X-Real-IP`: the
  session cookie is then marked `Secure`, and the login lockout (5 failed attempts → 10 minutes) uses
  the real client IP. Without a proxy in front, these headers could be set by clients – another reason
  not to expose the app directly. By default these headers are trusted from loopback and private networks;
  set `PAPIGATOR_TRUSTED_PROXIES` (comma-separated IPs or networks) to just your proxy if other devices
  in your network should not be able to fake them.
- Invites (Settings → Users → invite link or QR code) open `<public address>/app/#invite=…` and need
  `/app` and `/invites` – that works inside your network or over VPN. If you publish only `/s/*` as
  recommended, invite people while they are at home or on your VPN.
- Each user can delete their own account (Einstellungen → Konto & Geräte, and in the iPhone app). The
  last admin has to make someone else admin first.
- Passwords are stored with bcrypt; session and link tokens only as SHA-256 hashes; sessions expire
  after 90 days without use and can be revoked per device.
- `GET /ping` needs no login and only reveals whether the first-run setup is pending. All other API
  endpoints, including `/health`, require a login. The interactive API docs (`/docs`, `/redoc`,
  `/openapi.json`) are off unless you set `PAPIGATOR_API_DOCS=1`.

## Running without Docker

Python 3.12, plus the system packages `tesseract-ocr` (with `-deu`, `-eng`, `-osd`), `ghostscript`,
`qpdf`, `unpaper`, `pngquant` and `poppler-utils`:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
PAPIGATOR_DATA=/var/lib/papigator PAPIGATOR_TOKEN=… PAPIGATOR_SECRET_KEY=… \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
# public share pages, optional:
uvicorn app.share_app:app --host 0.0.0.0 --port 8001
```

Run both under a process manager of your choice (e.g. two systemd units).

Tests: `pip install -r requirements-dev.txt && python -m pytest` (browser tests:
`pip install -r requirements-e2e.txt && python -m playwright install chromium && python -m pytest e2e`).

## iPhone and iPad app

There is a companion app for iPhone and iPad (scanning with on-device OCR, search, deadlines widget,
Siri), coming to the App Store. It is a separate, closed-source program that talks to this server
over its HTTP API; the server is fully usable without it through the web app.

## License

The Papigator server – everything in this repository, including the web app in
`app/static/` – is licensed under the **GNU Affero General Public License v3.0** (AGPL-3.0-only);
see [LICENSE](LICENSE) and [NOTICE](NOTICE). If you run a modified version for others over a network,
you must offer them its source code.

The iPhone/iPad app is not part of this release and not covered by this license.

Papigator builds on many open-source projects; see [THIRD_PARTY.md](THIRD_PARTY.md).
