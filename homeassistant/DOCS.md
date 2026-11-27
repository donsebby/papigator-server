# Papigator

A self-hosted document archive for a household: drop in PDFs or scans, and Papigator runs OCR, makes everything
full-text searchable and keeps track of deadlines. Optional AI (Ollama or any OpenAI-compatible server) suggests
titles, senders, dates and tags. The web app and the AI speak German and English (option *language*).

## Setup

1. Fill in **Setup token** (any long random string). **Secret key** is optional but recommended
   (`openssl rand -base64 32`, keep a copy in your password manager).
2. Start the add-on and click **Open Web UI** (or open `http://<home-assistant-address>:8000/app`).
3. Click **Ersteinrichtung** (first-run setup), enter the setup token and create the first admin. After that the
   token no longer works; more users are added in the settings.
4. The iPhone/iPad app connects to the same address (`http://<home-assistant-address>:8000`). If Home Assistant
   keeps its default name, the app finds it by itself under *Im Netzwerk gefunden* (found in the network).

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
- The iPhone/iPad app looks for `http://homeassistant.local:8000` on the sign-in screen (Bonjour does not leave
  Home Assistant's internal add-on network). With a different host name, a VPN or another subnet, enter
  `http://<home-assistant-address>:8000` or scan the *App verbinden* (connect app) QR code from the web app's settings.
- Inbox and backup folder must be subfolders (e.g. `/share/papigator/…`), never `/share` itself.
- Updates arrive as new add-on versions. Take a Home Assistant backup first; database migrations run on start.

## Deadlines in Home Assistant

In the web app, *Einstellungen → Kalender-Abo* (settings → calendar subscription) creates a private link to all open
deadlines you can see. Home Assistant can use it twice:

- **As a calendar:** add the *Remote Calendar* integration with the `.ics` link.
- **As sensors:** the same link with `/summary.json` instead of `.ics` (shown below the calendar link) returns the
  number of open, overdue and upcoming (7 days) deadlines and the next one. Put the address in `secrets.yaml` – it
  works without a login – and add a REST sensor to `configuration.yaml`:

```yaml
# secrets.yaml
papigator_deadlines: http://homeassistant.local:8000/calendar/<token>/summary.json

# configuration.yaml
rest:
  - resource: !secret papigator_deadlines
    scan_interval: 900
    sensor:
      - name: Papigator open deadlines
        unique_id: papigator_open_deadlines
        value_template: "{{ value_json.open }}"
        json_attributes: [overdue, due_7_days, next, today]
      - name: Papigator overdue deadlines
        unique_id: papigator_overdue_deadlines
        value_template: "{{ value_json.overdue }}"
```

`next` holds `title`, `date`, `note` and `in_days` (negative when overdue), or is empty when nothing is open. A new
link in the settings makes the old one stop working; *Abschalten* turns both off.

Use the address that works from Home Assistant itself – the one from the settings, or the IP address of your Home
Assistant with port 8000. The title of the next deadline is stored in Home Assistant's history like any sensor
attribute; leave `next` out of `json_attributes` if you don't want that.

More: https://github.com/donsebby/papigator-server
