import logging
import sqlite3
import threading
from contextlib import contextmanager

from .config import DB_PATH

log = logging.getLogger("papigator.db")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    show_partner_in_overview INTEGER NOT NULL DEFAULT 0,  -- Option: Partner-Dokumente zwischen den eigenen
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label      TEXT,                                  -- z. B. "iPhone", "Web"
    ip         TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used  TEXT
);

-- Fehlversuche beim Login (Rate-Limit)
CREATE TABLE IF NOT EXISTS login_attempts (
    key        TEXT NOT NULL,        -- ip oder user:<name>
    at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(key, at);

-- Globale Einstellungen (key/value)
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Partner-Freigabe wie bei Immich: ganzer Pot, pro Richtung
CREATE TABLE IF NOT EXISTS partner_shares (
    from_user INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    to_user   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    can_edit  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (from_user, to_user)
);

CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      INTEGER REFERENCES users(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    original_name TEXT,
    sha256        TEXT UNIQUE,
    page_count    INTEGER DEFAULT 0,
    document_date TEXT,                      -- ISO-Datum, vom LLM oder Nutzer
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | processing | tagging | done | error
    llm_status    TEXT,                               -- pending | done | failed | skipped
    deleted_at    TEXT,                               -- Papierkorb
    error         TEXT,
    text          TEXT,                      -- Volltext (OCR oder vom Client)
    text_source   TEXT,                      -- client | ocr | pdf
    sender        TEXT,                      -- vom LLM extrahiert
    amount        REAL,
    currency      TEXT,
    summary       TEXT,
    due_date      TEXT,                      -- Frist (YYYY-MM-DD)
    due_note      TEXT,                      -- wofür: Zahlungsziel, Kündigungsfrist …
    due_done      INTEGER NOT NULL DEFAULT 0,
    due_assignee  INTEGER,                   -- wer sich kümmert (Nutzer-ID), NULL = alle
    due_repeat    TEXT,                      -- monthly | quarterly | yearly, NULL = einmalig
    due_last_done TEXT,                      -- zuletzt erledigt (wiederkehrende Frist)
    due_day       INTEGER                    -- Wunschtag einer Wiederholung, wenn am Monatsende gekürzt (31 → 28.02.)
);

CREATE TABLE IF NOT EXISTS tags (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT
);

CREATE TABLE IF NOT EXISTS document_tags (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    source      TEXT NOT NULL DEFAULT 'user',   -- user | rule | llm
    PRIMARY KEY (document_id, tag_id)
);

-- Gespeicherte Suchen („intelligente Ordner“) pro Nutzer; params = JSON mit q, tag, year, sender, due …
CREATE TABLE IF NOT EXISTS saved_views (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    params   TEXT NOT NULL DEFAULT '{}',
    position INTEGER NOT NULL DEFAULT 0
);

-- Freigabe-Links für Leute ohne Konto: ein Dokument, ein Ordner (Tag samt Unterordnern) oder eine
-- gespeicherte Suche (Filter als JSON-Schnappschuss). Gezeigt werden nur eigene Dokumente des Erstellers.
CREATE TABLE IF NOT EXISTS share_links (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token         TEXT NOT NULL UNIQUE,     -- SHA-256 des Tokens (das Original hat nur der Link)
    token_enc     TEXT,                     -- Token verschlüsselt, damit man den Link später kopieren kann
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,            -- doc | docs | tag | view | upload (Briefkasten)
    target        TEXT NOT NULL,            -- Dokument-ID(s), Tag-ID, JSON-Filter; bei upload Ziel-Ordner oder ""
    name          TEXT NOT NULL,
    password_hash TEXT,
    expires_at    TEXT,                     -- NULL = unbefristet
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_access   TEXT,
    access_count  INTEGER NOT NULL DEFAULT 0
);

-- Einladungen (16.11.2026): Admin lädt Familie/Freunde per Link oder QR-Code ein; nur die Prüfsumme des Tokens
-- liegt hier, einmal einlösbar, verfällt nach 7 Tagen. share_mode: Pot des Einladenden für die neue Person freigeben.
CREATE TABLE IF NOT EXISTS invites (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token        TEXT NOT NULL UNIQUE,      -- SHA-256 des Tokens
    created_by   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    is_admin     INTEGER NOT NULL DEFAULT 0,
    share_mode   TEXT NOT NULL DEFAULT 'none',   -- none | read | edit
    expires_at   TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    used_at      TEXT,
    used_by      INTEGER REFERENCES users(id) ON DELETE SET NULL
);

-- Dubletten-Suche: Paare, die der Nutzer mit „beide behalten“ als verschieden markiert hat (doc_a < doc_b)
CREATE TABLE IF NOT EXISTS duplicate_ignores (
    doc_a INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    doc_b INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    PRIMARY KEY (doc_a, doc_b)
);

-- Zusammengeführte Absender (13.11.2026): erkannte Schreibweise -> gewählter Name
CREATE TABLE IF NOT EXISTS sender_aliases (
    alias  TEXT PRIMARY KEY COLLATE NOCASE,
    target TEXT NOT NULL
);

-- Regeln: Stichwort im Text -> Tag
CREATE TABLE IF NOT EXISTS rules (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,                  -- einfacher Substring, case-insensitive
    tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE
);

-- Oberordner automatisch mittaggen (11.11.2026): bekommt ein Dokument „Strom“ (liegt in „Haus“), bekommt es
-- auch „Haus“. Mit PRAGMA recursive_triggers (in connect) greift das über beliebig viele Ebenen.
CREATE TRIGGER IF NOT EXISTS document_tags_parents AFTER INSERT ON document_tags BEGIN
  INSERT OR IGNORE INTO document_tags(document_id, tag_id, source)
  SELECT new.document_id, parent_id, 'parent' FROM tags WHERE id = new.tag_id AND parent_id IS NOT NULL;
END;

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title, text, sender,
    content='documents', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
  INSERT INTO documents_fts(rowid, title, text, sender)
  VALUES (new.id, new.title, new.text, new.sender);
END;
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, title, text, sender)
  VALUES ('delete', old.id, old.title, old.text, old.sender);
END;
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
  INSERT INTO documents_fts(documents_fts, rowid, title, text, sender)
  VALUES ('delete', old.id, old.title, old.text, old.sender);
  INSERT INTO documents_fts(rowid, title, text, sender)
  VALUES (new.id, new.title, new.text, new.sender);
END;
"""

_lock = threading.Lock()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.executescript(SCHEMA)
        # Migration für Datenbanken aus der ersten Version (ohne owner_id)
        cols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
        if "owner_id" not in cols:
            con.execute("ALTER TABLE documents ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        if "llm_status" not in cols:
            con.execute("ALTER TABLE documents ADD COLUMN llm_status TEXT")
        if "deleted_at" not in cols:
            con.execute("ALTER TABLE documents ADD COLUMN deleted_at TEXT")
        # Fristen (08.11.2026): Zahlungsziel, Kündigungsfrist … vom LLM vorgeschlagen oder von Hand gesetzt
        if "due_date" not in cols:
            con.execute("ALTER TABLE documents ADD COLUMN due_date TEXT")
            con.execute("ALTER TABLE documents ADD COLUMN due_note TEXT")
            con.execute("ALTER TABLE documents ADD COLUMN due_done INTEGER NOT NULL DEFAULT 0")
        # Fristen für die Familie (16.11.2026): wer kümmert sich, Wiederholung, zuletzt erledigt
        if "due_assignee" not in cols:
            con.execute("ALTER TABLE documents ADD COLUMN due_assignee INTEGER")
        if "due_repeat" not in cols:
            con.execute("ALTER TABLE documents ADD COLUMN due_repeat TEXT")
        if "due_last_done" not in cols:
            con.execute("ALTER TABLE documents ADD COLUMN due_last_done TEXT")
        if "due_day" not in cols:
            con.execute("ALTER TABLE documents ADD COLUMN due_day INTEGER")
        # Ordner (09.11.2026): ein Tag kann in einem anderen Tag liegen; die Dokumente bleiben flach getaggt
        tcols = {r[1] for r in con.execute("PRAGMA table_info(tags)")}
        if "parent_id" not in tcols:
            con.execute("ALTER TABLE tags ADD COLUMN parent_id INTEGER REFERENCES tags(id) ON DELETE SET NULL")
        # Wer einen Tag angelegt hat (13.11.2026): Tag-Namen anderer bleiben verborgen, eigene Ordner immer sichtbar.
        # Altbestand: Besitzer des ersten Dokuments mit dem Tag; Tags ohne Dokument bleiben NULL (für alle sichtbar).
        if "created_by" not in tcols:
            con.execute("ALTER TABLE tags ADD COLUMN created_by INTEGER")
            con.execute("UPDATE tags SET created_by = (SELECT d.owner_id FROM document_tags dt JOIN documents d ON d.id = dt.document_id "
                        "WHERE dt.tag_id = tags.id AND dt.source != 'parent' ORDER BY d.id LIMIT 1)")
        # Briefkasten (10.11.2026): von außen hochgeladen über einen Upload-Link, bis „Übernehmen“ zu prüfen
        if "ext_link_id" not in cols:
            con.execute("ALTER TABLE documents ADD COLUMN ext_link_id INTEGER")
            con.execute("ALTER TABLE documents ADD COLUMN ext_from TEXT")
            con.execute("ALTER TABLE documents ADD COLUMN ext_pending INTEGER NOT NULL DEFAULT 0")
        # Geld (13.11.2026): Art des Dokuments, Richtung des Betrags, Lastschrift, Rhythmus, § 35a-Anteil – vom LLM,
        # damit Summen ein Vorzeichen haben, Lastschriften keine Frist erzeugen und Verträge auch aus einem Beleg entstehen
        if "doc_kind" not in cols:
            con.execute("ALTER TABLE documents ADD COLUMN doc_kind TEXT")
            con.execute("ALTER TABLE documents ADD COLUMN direction TEXT")       # out | in | none, NULL = unbekannt
            con.execute("ALTER TABLE documents ADD COLUMN autopay INTEGER")      # 1 = Lastschrift/schon bezahlt
            con.execute("ALTER TABLE documents ADD COLUMN recurring TEXT")       # monatlich … jährlich, NULL = einmalig
            con.execute("ALTER TABLE documents ADD COLUMN tax_35a REAL")         # Arbeitskosten nach § 35a EStG
        if not con.execute("SELECT 1 FROM settings WHERE key='money_v1'").fetchone():
            # einmalig: fertige Dokumente bekommen die neuen Felder nachgetragen (nur diese – Korrekturen bleiben)
            con.execute("UPDATE documents SET status='tagging', llm_status='refill' "
                        "WHERE status='done' AND llm_status='done' AND deleted_at IS NULL")
            con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('money_v1', '1')")
            con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('money_v2', '1')")
        if not con.execute("SELECT 1 FROM settings WHERE key='money_v2'").fetchone():
            # einmalig (16.11.2026): Geld kam zurück – was ohne Geld-Felder erkannt wurde (keine Art, kein Betrag),
            # bekommt sie nachgetragen; Titel, Datum, Tags und Korrekturen bleiben
            con.execute("UPDATE documents SET status='tagging', llm_status='refill' WHERE status='done' "
                        "AND llm_status='done' AND deleted_at IS NULL AND doc_kind IS NULL AND amount IS NULL "
                        "AND COALESCE(text, '') != ''")
            con.execute("INSERT INTO settings(key, value) VALUES ('money_v2', '1')")
        lcols = {r[1] for r in con.execute("PRAGMA table_info(share_links)")}
        if "upload_count" not in lcols:
            con.execute("ALTER TABLE share_links ADD COLUMN upload_count INTEGER NOT NULL DEFAULT 0")
        scols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        if "ip" not in scols:
            con.execute("ALTER TABLE sessions ADD COLUMN ip TEXT")
        # Datum prüfen (16.11.2026): ältere Dokumente, deren Datum so nicht im Text steht (vom Sprachmodell erfunden
        # oder aus Laufzeiten/Fristen genommen) – markiert, nicht gelöscht; sie stehen unter „Zu prüfen“
        if "date_check" not in cols:
            con.execute("ALTER TABLE documents ADD COLUMN date_check INTEGER NOT NULL DEFAULT 0")
        # Geld (17.11.2026): Kontoumsätze aus CSV-Exporten – eigenes Modul app/bank.py
        con.executescript("""
            CREATE TABLE IF NOT EXISTS bank_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, bank TEXT,
                owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS bank_access (
                account_id INTEGER NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY (account_id, user_id));
            CREATE TABLE IF NOT EXISTS bank_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
                filename TEXT, created_by INTEGER, count INTEGER, first TEXT, last TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS bank_tx (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE,
                import_id INTEGER REFERENCES bank_imports(id) ON DELETE CASCADE,
                booked TEXT NOT NULL, cents INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'EUR',
                counterparty TEXT, purpose TEXT, category TEXT, cat_source TEXT,
                document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
                fp TEXT NOT NULL, UNIQUE (account_id, fp));
            CREATE INDEX IF NOT EXISTS bank_tx_booked ON bank_tx(account_id, booked);
            CREATE TABLE IF NOT EXISTS bank_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                pattern TEXT NOT NULL, category TEXT NOT NULL, UNIQUE (created_by, pattern));
            -- Jährliche Kosten (24.11.2026, app/yearly.py): Rücklagen-Topf je Person und von Hand markierte Posten
            -- (Händler + Betrag; yearly=1 gehört dazu, 0 nie) – Fremdschlüssel räumen beim Löschen mit auf
            CREATE TABLE IF NOT EXISTS bank_pot (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                account_id INTEGER NOT NULL REFERENCES bank_accounts(id) ON DELETE CASCADE);
            -- Kalender-Abo (25.11.2026, app/calfeed.py): Token nur als Hash (Abfrage) und verschlüsselt (Anzeigen)
            CREATE TABLE IF NOT EXISTS calendar_feeds (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                token_enc TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
            -- Notizen am Dokument (25.11.2026, app/notes.py)
            CREATE TABLE IF NOT EXISTS document_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')));
            CREATE INDEX IF NOT EXISTS idx_document_notes_doc ON document_notes(document_id);
            -- „kein Topf“ ausdrücklich gewählt (24.11.2026): sonst schlüge ein Konto namens „Jährlich“ wieder vor
            CREATE TABLE IF NOT EXISTS bank_pot_off (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE);
            CREATE TABLE IF NOT EXISTS bank_yearly_marks (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                mkey TEXT NOT NULL, cents INTEGER NOT NULL, yearly INTEGER NOT NULL,
                PRIMARY KEY (user_id, mkey, cents));
        """)
        # Buchungsart und Kategorie der Bank mitspeichern (18.11.2026) – Pocket-Überträge stehen oft nur dort; Altbestand
        # einmal neu einsortieren (nur, was nicht von Hand oder per Regel sortiert ist)
        bacols = {r[1] for r in con.execute("PRAGMA table_info(bank_accounts)")}
        if "number_hash" not in bacols:   # eigenes Konto je Zeile (C24-Export mit Pockets/Sparkonten, 18.11.2026)
            con.execute("ALTER TABLE bank_accounts ADD COLUMN number_hash TEXT")
            con.execute("ALTER TABLE bank_accounts ADD COLUMN number_tail TEXT")
            con.execute("ALTER TABLE bank_imports ADD COLUMN batch TEXT")
        btcols = {r[1] for r in con.execute("PRAGMA table_info(bank_tx)")}
        if "kind" not in btcols:
            con.execute("ALTER TABLE bank_tx ADD COLUMN kind TEXT")
            con.execute("ALTER TABLE bank_tx ADD COLUMN bank_category TEXT")
            con.execute("UPDATE bank_tx SET category=NULL, cat_source=NULL WHERE cat_source='builtin' AND category='Einnahmen' "
                        "AND cents > 0 AND NOT (lower(COALESCE(counterparty, '') || ' ' || COALESCE(purpose, '')) GLOB "
                        "'*gehalt*' OR lower(COALESCE(purpose, '')) GLOB '*lohn*' OR lower(COALESCE(purpose, '')) GLOB '*rente*')")
        # einmal neu einsortieren, wenn die eingebauten Händler besser geworden sind (Abos wie Apple/Anthropic statt
        # „Freizeit“ der Bank, 18.11.2026) – von Hand oder per Regel Sortiertes bleibt
        if not con.execute("SELECT 1 FROM settings WHERE key='bank_recat_v1'").fetchone():
            from .bank import recategorize   # späte Bindung
            con.row_factory = sqlite3.Row
            n = recategorize(con, [r[0] for r in con.execute("SELECT id FROM bank_accounts")])
            con.row_factory = None
            con.execute("INSERT INTO settings(key, value) VALUES ('bank_recat_v1', ?)", (str(n),))
        # Neue Kategorie „Online-Shopping“ (18.11.2026): offene und automatisch sortierte Buchungen einmal nachziehen
        if not con.execute("SELECT 1 FROM settings WHERE key='bank_recat_v2'").fetchone():
            from .bank import recategorize   # späte Bindung
            con.row_factory = sqlite3.Row
            n = recategorize(con, [r[0] for r in con.execute("SELECT id FROM bank_accounts")])
            con.row_factory = None
            con.execute("INSERT INTO settings(key, value) VALUES ('bank_recat_v2', ?)", (str(n),))
        # Gewährleistung/Garantie (17.11.2026): Monate (NULL = nie geprüft, 0 = keine), Art, Ende – siehe processing
        if "warranty_months" not in cols:
            con.execute("ALTER TABLE documents ADD COLUMN warranty_months INTEGER")
            con.execute("ALTER TABLE documents ADD COLUMN warranty_kind TEXT")
            con.execute("ALTER TABLE documents ADD COLUMN warranty_until TEXT")
        if not con.execute("SELECT 1 FROM settings WHERE key='warranty_v1'").fetchone():
            # einmalig: erkannte Rechnungen (Ausgaben) bekommen Gewährleistung/Garantie nachgetragen – nur diese Felder
            con.execute("UPDATE documents SET status='tagging', llm_status='refill_w' WHERE status='done' "
                        "AND llm_status='done' AND deleted_at IS NULL AND doc_kind='rechnung' "
                        "AND COALESCE(direction, '') <> 'in' AND warranty_months IS NULL AND COALESCE(text, '') != ''")
            con.execute("INSERT INTO settings(key, value) VALUES ('warranty_v1', '1')")
        # Ablaufdaten (Ausweise, Karten), Wartungen (HU, Heizung …), Erstattungen (GOÄ) – 17.11.2026, siehe processing
        if "expires_on" not in cols:
            for c in ("expires_on TEXT", "expires_what TEXT", "service_what TEXT", "service_next TEXT",
                      "refund_state TEXT", "refund_doc INTEGER"):
                con.execute(f"ALTER TABLE documents ADD COLUMN {c}")
        if not con.execute("SELECT 1 FROM settings WHERE key='extras_v1'").fetchone():
            # einmalig: Dokumente, die nach Ausweis/Karte oder Prüfung/Wartung aussehen, bekommen die Felder nachgetragen
            n = con.execute(
                "UPDATE documents SET status='tagging', llm_status='refill_w' WHERE status='done' AND llm_status='done' "
                "AND deleted_at IS NULL AND expires_on IS NULL AND service_what IS NULL AND ("
                + " OR ".join("lower(COALESCE(text, '')) LIKE ?" for _ in _EXTRAS_HINTS) + ")",
                [f"%{w}%" for w in _EXTRAS_HINTS]).rowcount
            from .processing import apply_refund   # späte Bindung: processing importiert db
            # erst die Arztrechnungen der letzten zwei Jahre öffnen, dann die Abrechnungen/Bescheide sie schließen lassen
            # (apply_refund liest Spalten per Namen – diese Verbindung liefert sonst Tupel)
            con.row_factory = sqlite3.Row
            for kind in ("= 'rechnung' AND COALESCE(document_date, created_at) >= date('now', '-2 years')",
                         "IS NOT 'rechnung'"):
                for r in con.execute(f"SELECT id FROM documents WHERE deleted_at IS NULL AND doc_kind {kind} "
                                     "AND COALESCE(text, '') != '' ORDER BY COALESCE(document_date, created_at), id").fetchall():
                    apply_refund(con, r[0])
            con.row_factory = None
            con.execute("INSERT INTO settings(key, value) VALUES ('extras_v1', ?)", (str(n),))
            log.info("Zusatzfelder: %s Dokument(e) zum Nachtragen vorgemerkt", n)
        # Dokumentart allgemein, Empfänger, Nummern (26.11.2026, processing.kind_fields): Bestand einmalig nachtragen –
        # nur diese Felder (refill_k), still: Status bleibt „done“ (kein „Tagging“ überall, „Zu prüfen“ bleibt), der
        # Worker nimmt sie nur, wenn sonst nichts zu tun ist (worker.llm_loop)
        if "recipient" not in cols:
            con.execute("ALTER TABLE documents ADD COLUMN recipient TEXT")
            con.execute("ALTER TABLE documents ADD COLUMN refs TEXT")   # JSON: contract/customer/case/invoice/other
        if not con.execute("SELECT 1 FROM settings WHERE key='kinds_v1'").fetchone():
            n = con.execute("UPDATE documents SET llm_status='refill_k' WHERE status='done' "
                            "AND llm_status='done' AND deleted_at IS NULL AND COALESCE(text, '') != ''").rowcount
            con.execute("INSERT INTO settings(key, value) VALUES ('kinds_v1', ?)", (str(n),))
            log.info("Dokumentart/Empfänger/Nummern: %s Dokument(e) zum Nachtragen vorgemerkt", n)
        # „Hat kein Datum“ (17.11.2026): von Hand bestätigt – steht dann nicht mehr unter „Zu prüfen“
        if "no_date" not in cols:
            con.execute("ALTER TABLE documents ADD COLUMN no_date INTEGER NOT NULL DEFAULT 0")
        # Von Hand geänderte Felder (27.11.2026, kommagetrennt): die KI überschreibt sie nicht mehr – weder beim
        # Nachholen fehlgeschlagener Erkennung noch, wenn jemand während des Erkennens bearbeitet. „Neu erkennen“ leert sie.
        if "hand_fields" not in cols:
            con.execute("ALTER TABLE documents ADD COLUMN hand_fields TEXT")
        if not con.execute("SELECT 1 FROM settings WHERE key='date_check_v1'").fetchone():
            from .processing import flag_doubtful_dates   # späte Bindung: processing importiert db
            n = flag_doubtful_dates(con)
            con.execute("INSERT INTO settings(key, value) VALUES ('date_check_v1', ?)", (str(n),))
        else:
            from .processing import recheck_flagged_dates
            recheck_flagged_dates(con)
        # Stichprobe 27.11.2026: Empfänger aus dem Briefkopf, Nummern des Absenders, § 35a Schornsteinfeger, HU-Termin,
        # 16 % MwSt außerhalb 2020 – einmal über den Bestand
        if not con.execute("SELECT 1 FROM settings WHERE key='recheck_v2'").fetchone():
            from .processing import recheck_v2   # späte Bindung: processing importiert db
            con.row_factory = sqlite3.Row
            try:
                n = recheck_v2(con)
            finally:
                con.row_factory = None
            con.execute("INSERT INTO settings(key, value) VALUES ('recheck_v2', ?)", (str(n),))
        # Einmalig (27.11.2026): ohne KI abgelegte Dokumente ohne Datum bekommen eins, wenn es eindeutig im Text steht
        if not con.execute("SELECT 1 FROM settings WHERE key='date_noai_v1'").fetchone():
            from .processing import date_without_ai   # späte Bindung: processing importiert db
            ids = [r[0] for r in con.execute("SELECT id FROM documents WHERE llm_status='skipped' AND status='done' "
                                             "AND document_date IS NULL AND no_date=0 AND deleted_at IS NULL")]
            n = sum(date_without_ai(con, i) for i in ids)
            if n:
                log.info("Datum ohne KI nachgetragen: %d Dokumente", n)
            con.execute("INSERT INTO settings(key, value) VALUES ('date_noai_v1', ?)", (str(n),))
        # Einmalig (27.11.2026): ohne KI abgelegte Dokumente bekommen den Absender aus dem eigenen Bestand und
        # statt eines bloßen Dateinamens den Titel „Absender · Datum“
        if not con.execute("SELECT 1 FROM settings WHERE key='sender_noai_v1'").fetchone():
            from .processing import sender_title_without_ai
            ids = [r[0] for r in con.execute("SELECT id FROM documents WHERE llm_status='skipped' AND status='done' "
                                             "AND deleted_at IS NULL AND text IS NOT NULL")]
            known: dict = {}   # Absender je Pot nur einmal laden (sonst Minuten bei großem Bestand)
            n = sum(sender_title_without_ai(con, i, known) for i in ids)
            if n:
                log.info("Absender/Titel ohne KI nachgetragen: %d Dokumente", n)
            con.execute("INSERT INTO settings(key, value) VALUES ('sender_noai_v1', ?)", (str(n),))
        if not con.execute("SELECT 1 FROM settings WHERE key='tags_parents_v1'").fetchone():
            add_parent_tags(con)   # einmalig: bestehende Dokumente bekommen ihre Oberordner
            con.execute("INSERT INTO settings(key, value) VALUES ('tags_parents_v1', '1')")
        # Familie (16.11.2026): Personen (Kinder ohne Konto) sind Tags mit Regel auf den Vornamen, die Notfallmappe
        # ein besonderer Tag. tags.kind: NULL | person | emergency – „person“, solange jemand die Person angelegt hat.
        if "kind" not in tcols:
            con.execute("ALTER TABLE tags ADD COLUMN kind TEXT")
        # Regeln mit Ersteller (Personen-Regeln) greifen nur bei Dokumenten, die der Ersteller bearbeiten darf;
        # word=1: nur ganze Wörter („Ben“ nicht in „neben“). Alte Regeln: NULL/0 wie bisher, für alle Dokumente.
        rcols = {r[1] for r in con.execute("PRAGMA table_info(rules)")}
        if "created_by" not in rcols:
            con.execute("ALTER TABLE rules ADD COLUMN created_by INTEGER REFERENCES users(id) ON DELETE CASCADE")
        if "word" not in rcols:
            con.execute("ALTER TABLE rules ADD COLUMN word INTEGER NOT NULL DEFAULT 0")
        con.executescript("""
-- Wer welche Person angelegt hat: Namen sieht nur, wer den Bestand des Anlegenden sehen darf
CREATE TABLE IF NOT EXISTS persons (
    tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (tag_id, user_id)
);
-- Aufbewahrungsfristen pro Ordner, vom Admin eingestellt (sonst gelten die Vorgaben in cleanup.py);
-- years NULL = nie zum Aufräumen vorschlagen
CREATE TABLE IF NOT EXISTS retention (
    tag_id INTEGER PRIMARY KEY REFERENCES tags(id) ON DELETE CASCADE,
    years  INTEGER
);
-- Demo (PAPIGATOR_DEMO): Uploads/Verarbeitungen pro Nutzer, für das Tageslimit (app/demo.py)
CREATE TABLE IF NOT EXISTS demo_uploads (
    user_id INTEGER NOT NULL,
    at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_demo_uploads ON demo_uploads(user_id, at);
""")
        # Bezahlen per QR (26.11.2026, app/payment.py): pro Konto einschalten, Standard aus
        ucols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
        if "pay_qr" not in ucols:
            con.execute("ALTER TABLE users ADD COLUMN pay_qr INTEGER NOT NULL DEFAULT 0")
        # Willkommen/„Was ist neu“ (25.11.2026, app/whatsnew.py): zuletzt gesehene Ausgabe pro Konto. Beim Einführen
        # bekommen bestehende Konten „0“ – sie kennen die App, sehen also die Neuerungen statt der Tour.
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='whatsnew_seen'").fetchone():
            con.execute("CREATE TABLE whatsnew_seen (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, "
                        "seen TEXT NOT NULL)")
            con.execute("INSERT INTO whatsnew_seen(user_id, seen) SELECT id, '0' FROM users")
        # „Los geht’s“ (26.11.2026, app/onboarding.py): Konten, die die Karte ausgeblendet haben. Beim Einführen gelten
        # alle bestehenden Konten als ausgeblendet – sie kennen die App; nur neue Konten sehen die ersten Schritte.
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='onboarding_dismissed'").fetchone():
            con.execute("CREATE TABLE onboarding_dismissed (user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, "
                        "at TEXT NOT NULL DEFAULT (datetime('now')))")
            con.execute("INSERT INTO onboarding_dismissed(user_id) SELECT id FROM users")
        # Wochenübersicht per Mail (25.11.2026, app/digest.py): pro Konto an/aus, Wochentag (0 = Montag), Adresse;
        # last_week = zuletzt gesendete ISO-Woche („2026-W48“) gegen Doppelversand, last_try für Wiederholungen
        con.execute("""CREATE TABLE IF NOT EXISTS digest_prefs (
            user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            enabled    INTEGER NOT NULL DEFAULT 0,
            weekday    INTEGER NOT NULL DEFAULT 0,
            email      TEXT NOT NULL DEFAULT '',
            last_week  TEXT,
            last_try   TEXT,
            last_sent  TEXT,
            last_error TEXT)""")
        # Sprache der Wochenübersicht pro Konto (de | en); NULL = Sprache des Servers (Einstellung „Sprache der Erkennung“).
        # Web-App und iOS-App schicken beim Speichern ihre Oberflächensprache mit.
        if "lang" not in {r[1] for r in con.execute("PRAGMA table_info(digest_prefs)")}:
            con.execute("ALTER TABLE digest_prefs ADD COLUMN lang TEXT")
        global _trigram
        _trigram = setup_trigram(con)
        con.execute("CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_documents_owner ON documents(owner_id)")


def add_parent_tags(con) -> int:
    """Allen Dokumenten die Oberordner ihrer Tags geben (nach dem Verschieben eines Ordners oder einmalig)."""
    return con.execute(
        "WITH RECURSIVE anc(doc, id, depth) AS ("
        " SELECT dt.document_id, t.parent_id, 1 FROM document_tags dt JOIN tags t ON t.id = dt.tag_id WHERE t.parent_id IS NOT NULL"
        " UNION SELECT anc.doc, t.parent_id, anc.depth + 1 FROM anc JOIN tags t ON t.id = anc.id"
        " WHERE t.parent_id IS NOT NULL AND anc.depth < 50) "
        "INSERT OR IGNORE INTO document_tags(document_id, tag_id, source) SELECT doc, id, 'parent' FROM anc").rowcount


def refresh_parent_tags(con) -> None:
    """Automatisch gesetzte Oberordner (source='parent') neu berechnen – nach Verschieben oder Löschen eines
    Ordners, damit z. B. ein rückgängig gemachtes Verschieben keine falschen Oberordner zurücklässt.
    Selbst gesetzte Tags (user/llm/rule) bleiben unberührt."""
    con.execute("DELETE FROM document_tags WHERE source='parent'")
    add_parent_tags(con)


@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA recursive_triggers=ON")   # Oberordner über mehrere Ebenen (document_tags_parents)
    # Gelöschtes überschreiben statt nur freigeben (27.11.2026) – sonst blieb der Text „endgültig gelöschter“ Dokumente
    # in freien Seiten der Datenbank und wanderte mit in jede Sicherung
    con.execute("PRAGMA secure_delete=ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def fts_terms(q: str) -> tuple[list[str], list[str]]:
    """Nutzereingabe in FTS5-Prefix-Terme zerlegen: (einschließen, ausschließen).
    Anführungszeichen fliegen raus, sonst bricht die FTS5-Syntax."""
    include, exclude = [], []
    for raw in q.split():
        neg = raw.startswith("-")
        w = raw.lstrip("-").replace('"', "")
        if w:
            (exclude if neg else include).append(f'"{w}"*')
    return include, exclude


def fts_query(q: str) -> str:
    """Nutzereingabe in eine tolerante FTS5-Query umwandeln: jedes Wort als Prefix.
    Ein Minus davor schließt aus: „sam -alex“ findet Dokumente mit Sam, aber ohne Alex.
    FTS5 kennt NOT nur zwischen zwei Ausdrücken; nur Ausschlüsse liefern hier "" –
    dafür gibt es `fts_exclude_query` (Aufrufer filtert dann per NOT IN)."""
    include, exclude = fts_terms(q)
    if not include:
        return ""
    query = " ".join(include)
    for w in exclude:
        query += f" NOT {w}"
    return query


# Suche (13.11.2026): Wörter auch mitten in zusammengesetzten Wörtern finden („Steuerbescheid“ in
# „Einkommensteuerbescheid“) – über einen zweiten FTS5-Index mit Trigrammen, falls SQLite ihn kann (ab 3.34).
_trigram: bool | None = None
TRIGRAM_MIN = 4   # kürzere Wörter nur am Wortanfang, sonst findet „Auto“ jeden „Autor“ und „VHS“ zu viel

# Wenige, eindeutige Gleichsetzungen aus Haushaltsunterlagen: Wer das eine sucht, meint das andere mit
SYNONYMS = [
    ("nebenkosten", "betriebskosten"),
    ("kredit", "darlehen"),
    ("handy", "mobilfunk"),
    ("kfz", "kraftfahr"),
    ("kita", "kindertagesst", "kindergarten"),
    ("mahnung", "zahlungserinnerung"),
]


def setup_trigram(con) -> bool:
    """Teilwort-Index anlegen (einmalig mit „rebuild“ befüllen) samt Triggern; False, wenn SQLite das nicht kann."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='documents_tri'").fetchone():
        for tok in ("trigram remove_diacritics 1", "trigram"):   # remove_diacritics erst ab SQLite 3.45
            try:
                con.execute("CREATE VIRTUAL TABLE documents_tri USING fts5(title, text, sender, "
                            f"content='documents', content_rowid='id', tokenize='{tok}')")
                break
            except sqlite3.OperationalError:
                continue
        else:
            return False
        con.execute("INSERT INTO documents_tri(documents_tri) VALUES('rebuild')")
    con.executescript("""
CREATE TRIGGER IF NOT EXISTS documents_tri_ai AFTER INSERT ON documents BEGIN
  INSERT INTO documents_tri(rowid, title, text, sender) VALUES (new.id, new.title, new.text, new.sender);
END;
CREATE TRIGGER IF NOT EXISTS documents_tri_ad AFTER DELETE ON documents BEGIN
  INSERT INTO documents_tri(documents_tri, rowid, title, text, sender) VALUES ('delete', old.id, old.title, old.text, old.sender);
END;
CREATE TRIGGER IF NOT EXISTS documents_tri_au AFTER UPDATE OF title, text, sender ON documents BEGIN
  INSERT INTO documents_tri(documents_tri, rowid, title, text, sender) VALUES ('delete', old.id, old.title, old.text, old.sender);
  INSERT INTO documents_tri(rowid, title, text, sender) VALUES (new.id, new.title, new.text, new.sender);
END;
""")
    return True


def has_trigram(con) -> bool:
    global _trigram
    if _trigram is None:
        _trigram = bool(con.execute("SELECT 1 FROM sqlite_master WHERE name='documents_tri'").fetchone())
    return _trigram


def _variants(word: str) -> list[str]:
    """Schreibweisen eines Suchworts: „kuendigung“ → auch „kundigung“ (Umlaute fallen im Index weg),
    „strasse“ → „straße“, dazu die Gleichsetzungen aus SYNONYMS."""
    w = word.lower()
    out = [w]
    folded = w.replace("ae", "a").replace("oe", "o").replace("ue", "u")
    if folded != w:
        out.append(folded)
    if "ss" in w:
        out.append(w.replace("ss", "ß"))
    for group in SYNONYMS:
        if w in group or folded in group:
            out += [g for g in group if g not in out]
    return out


def search_where(con, q: str) -> tuple[list[str], list, str, str]:
    """Bedingungen für die Volltextsuche: (where, params, Ausschnitt-Query Wortindex, Ausschnitt-Query Teilwörter).
    Jedes Wort muss vorkommen – am Wortanfang, mitten im Wort (ab TRIGRAM_MIN Zeichen, keine reinen Zahlen)
    oder als Tag/Ordner. „Kfz-Versicherung“ zählt als zwei Wörter. Ein Minus davor schließt aus."""
    where, params, snip_w, snip_t = [], [], [], []
    tri = has_trigram(con)
    include, _ = fts_terms(q)
    words = []
    for t in include:
        words += [p for p in t[1:-2].replace("-", " ").replace("/", " ").split() if p]
    for w in words:
        vs = _variants(w)
        wq = " OR ".join(f'"{v}"*' for v in vs)
        ors = ["d.id IN (SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?)"]
        args = [wq]
        snip_w.append(wq)
        long = [v for v in vs if len(v) >= TRIGRAM_MIN and not v.isdigit()]
        if tri and long:
            tq = " OR ".join(f'"{v}"' for v in long)
            ors.append("d.id IN (SELECT rowid FROM documents_tri WHERE documents_tri MATCH ?)")
            args.append(tq)
            snip_t.append(tq)
        like = w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        ors.append("d.id IN (SELECT dt.document_id FROM document_tags dt JOIN tags t ON t.id = dt.tag_id "
                   "WHERE t.name LIKE ? ESCAPE '\\')")
        args.append(like)
        where.append("(" + " OR ".join(ors) + ")")
        params += args
    xq = fts_exclude_query(q)
    if xq:
        where.append("d.id NOT IN (SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?)")
        params.append(xq)
    return where, params, " OR ".join(snip_w), " OR ".join(snip_t)


def fts_exclude_query(q: str) -> str:
    """Nur die Ausschlüsse als ODER-Query („-a -b“ → "a"* OR "b"*), leer wenn keine."""
    return " OR ".join(fts_terms(q)[1])


# Wörter, an denen das Nachtragen der Zusatzfelder Ausweise/Karten und Prüfberichte/Wartungen erkennt (klein)
_EXTRAS_HINTS = ("gültig bis", "gueltig bis", "valid thru", "valid until", "ablaufdatum", "personalausweis", "reisepass",
                 "führerschein", "aufenthaltstitel", "hauptuntersuchung", "prüfbericht", "wartung", "schornstein",
                 "kaminkehr", "rauchmelder", "bahncard")


def get_setting(con, key: str, default: str | None = None) -> str | None:
    r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(con, key: str, value: str | None) -> None:
    con.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
