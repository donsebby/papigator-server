# Changelog

## 2026.11.28.1

- Customize “Today” per account: hide any section, e.g. the emergency folder (web: Settings → Customize Today)
- New cards on “Today”: time to cancel (contracts whose notice period ends within 8 weeks, with the yearly cost),
  tax year (§ 35a labour costs, donations), drop box, a year ago, recently viewed, this week, and a system status
  for admins (only when something is wrong)
- Web app: drag a document onto another folder in the folder view; picking a tag from the suggestions adds it
  without Enter; “Overview” closes a document in one step after a double click; drop-down lists stay readable
  with a light theme on a dark Windows desktop

## 2026.11.27.1

- AppArmor profile for the add-on (higher security rating)
- Deadlines as Home Assistant sensors: the calendar subscription link with `/summary.json` (see the documentation)
- Without AI: dates, senders and titles are taken from the text when they are unambiguous – otherwise left empty;
  tags that all recent letters from the same sender carry are added (once for existing documents, too); much
  faster with large archives
- The AI no longer overwrites fields you changed by hand
- Security fixes: hidden tags and folders reveal nothing, no tokens or search terms in the access log, mail
  certificates are checked, restoring a backup does not revive revoked sessions or links, size limits for TIFF and
  mail attachments
- The iPhone app also finds the add-on at homeassistant.local:8000
- Web app: keyboard support for cards and chips, Enter runs the main action in dialogs, clearer error messages,
  better layout on phones

## 2026.11.26.1

- Rebuilt web app: one field for searching and asking questions
- Pay by QR code (EPC/GiroCode, opt-in per account)
- Connect the iPhone/iPad app with a QR code from the web app
- Onboarding for new accounts, photo upload (JPEG/PNG/TIFF become PDFs), Paperless-ngx import
- English interface and English AI recognition (add-on option *language*)
- Weekly email digest (per account, needs SMTP in the settings)
- Document type, recipient and reference numbers for every document
- Emergency folder checklist: what belongs in it, what is already there, suggestions from your archive
- Worker fixes

## 2026.11.25.3

- The progress of filling in older documents only shows while the AI is reachable
- An unexpected error while filling in an older document no longer retries the same document in a loop

## 2026.11.25.2

- Document type, recipient and contract/customer/case numbers for every document, not only invoices – taken over
  only when they appear in the text; editable and filterable by type
- Existing documents are filled in quietly in the background while the AI is idle

## 2026.11.25.1

- Welcome tour on first start and "What's new" after updates (once per account)
- Notes on documents, calendar subscription for all open deadlines, cancellation letter helper
- Warranty reminders under "Coming up", profile pictures, update notice for admins
- Assign several deadlines at once, pages in the tile view, family view in the web app

## 2026.11.25

- Home Assistant backups stop the add-on for a moment, so the database copy is consistent; Papigator's own backup
  is off by default (a copy under `/share` would be readable via Samba)
- Inbox and backup folder must be subfolders, never `/share` itself
- Fixed server version per add-on version instead of `latest`
- The inbox no longer follows symbolic links

## 2026.11.24

- First version of the Home Assistant add-on
