# Papigator

A self-hosted document archive for a household: drop in PDFs or scans, and Papigator runs OCR, makes everything
full-text searchable and keeps track of deadlines. Optional AI (Ollama or any OpenAI-compatible server) suggests
titles, senders, dates and tags. The web app's interface is currently in German.

## Setup

1. Fill in **Setup token** (any long random string). **Secret key** is optional but recommended
   (`openssl rand -base64 32`, keep a copy in your password manager).
2. Start the add-on and click **Open Web UI** (or open `http://<home-assistant-address>:8000/app`).
3. Click **Ersteinrichtung** (first-run setup), enter the setup token and create the first admin. After that the
   token no longer works; more users are added in the settings.
4. The iPhone/iPad app connects to the same address (`http://<home-assistant-address>:8000`).

## Where things live

| What | Where |
|---|---|
| Database, PDFs, thumbnails | add-on data – included in Home Assistant backups |
| Scanner inbox | `/share/papigator/inbox/<username>/` – via the Samba add-on: `\\<home-assistant>\share\papigator\inbox` |
| Backups | Home Assistant backups (the add-on pauses for a moment so the database copy is consistent) |
| Papigator's own nightly backup | off by default – set *Backup folder*, e.g. `/share/papigator/backup` (unencrypted, readable via Samba) |

Files dropped into the inbox are imported for that user and then removed; files that fail move to `_fehler/`.

## Good to know

- Keep port 8000 inside your network. To reach Papigator from outside, use a VPN – don't forward the port.
- Small devices (e.g. Home Assistant Green, Raspberry Pi) handle the archive and OCR fine; a local AI model needs a
  bigger machine – point *AI server URL* at one in your network, or leave it empty.
- Public share links (`/s/…`) need the separate share service, which this add-on does not run.
- The iPhone/iPad app cannot find the add-on automatically (Bonjour does not leave Home Assistant's internal add-on
  network) – enter `http://<home-assistant-address>:8000` or scan the QR code from the web app.
- Inbox and backup folder must be subfolders (e.g. `/share/papigator/…`), never `/share` itself.
- Updates arrive as new add-on versions. Take a Home Assistant backup first; database migrations run on start.

More: https://github.com/donsebby/papigator-server
