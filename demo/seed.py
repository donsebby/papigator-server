"""Demoserver befüllen: frische, leere Instanz → erfundener Haushalt Alex & Sam Muster mit allem, was die App zeigt.

Läuft gegen einen Server, der nur lokal lauscht und noch keinen Nutzer hat (Bootstrap-Token), z. B. aus
demo/demo-run.sh. Ohne Sprachmodell: Titel, Absender, Daten, Beträge und Ordner kommen aus meta.json
(einmal mit dem echten Modell erkannt), alle Daten wandern mit dem Kalender mit (shift.py).

    PAPIGATOR_URL=http://127.0.0.1:8000 PAPIGATOR_TOKEN=<Bootstrap> DEMO_PASSWORD=<für alex und sam> \\
    PAPIGATOR_DATA=/data python demo/seed.py <Ordner mit docs.json und pdf/ aus make_docs.py>

Konten: „alex“ und „sam“ (keine Admins, gleiches Passwort, gegenseitig mit Schreibrecht geteilt). Dazu ein Admin
„verwaltung“ mit Zufallspasswort, das niemand erfährt – Admins könnten in einer öffentlichen Demo sonst KI-, Mail-
oder Sicherungs-Einstellungen auf fremde Adressen richten.
"""
import json
import os
import secrets
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))
import make_bank  # noqa: E402
import shift  # noqa: E402
from app.processing import warranty_end  # noqa: E402

B = os.environ.get("PAPIGATOR_URL", "http://127.0.0.1:8000")
SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "papigator-demo")
DB = Path(os.environ.get("PAPIGATOR_DATA", "/data")) / "papigator.db"
PW = os.environ["DEMO_PASSWORD"]
META = json.loads((Path(__file__).resolve().parent / "meta.json").read_text())
COLS = ["title", "document_date", "sender", "amount", "currency", "summary", "due_date", "due_note", "due_done",
        "doc_kind", "direction", "autopay", "recurring", "tax_35a"]
c = httpx.Client(base_url=B, timeout=120)
today = shift.today()


def ok(r):
    if r.status_code >= 400:
        sys.exit(f"{r.request.method} {r.request.url.path}: {r.status_code} {r.text[:200]}")
    return r


def login(u, pw):
    t = ok(c.post("/auth/login", json={"username": u, "password": pw, "label": "Demo-Befüllung"})).json()["token"]
    c.cookies.clear()
    return {"Authorization": f"Bearer {t}"}


# --- Konten und Freigaben
admin_pw = secrets.token_urlsafe(24)
ok(c.post("/users", json={"username": "verwaltung", "display_name": "Verwaltung", "password": admin_pw},
          headers={"Authorization": f"Bearer {os.environ['PAPIGATOR_TOKEN']}"}))
ADM = login("verwaltung", admin_pw)
for u, n in (("alex", "Alex"), ("sam", "Sam")):
    ok(c.post("/users", json={"username": u, "display_name": n, "password": PW, "is_admin": False}, headers=ADM))
# Testkonto für Apples Prüfer: „Konto löschen“ ausprobieren, ohne die geteilte Demo kaputt zu machen
# (alex und sam dürfen sich in der Demo nicht löschen, config.DEMO_KEEP_USERS)
ok(c.post("/users", json={"username": "loeschtest", "display_name": "Testkonto", "password": PW, "is_admin": False},
          headers=ADM))
H = {"alex": login("alex", PW), "sam": login("sam", PW)}
ids = {u: ok(c.get("/auth/me", headers=h)).json()["id"] for u, h in H.items()}
ok(c.put("/shares", json={"user_id": ids["sam"], "can_edit": True}, headers=H["alex"]))
ok(c.put("/shares", json={"user_id": ids["alex"], "can_edit": True}, headers=H["sam"]))

# --- Dokumente hochladen (mit Text: kein OCR nötig) und warten, bis die Verarbeitung durch ist
docs = json.loads((SRC / "docs.json").read_text())
assert len(docs) == len(META), "meta.json passt nicht zu make_docs.py – meta.json neu erzeugen"
for x, m in zip(docs, META):
    assert x["file"] == m["file"], (x["file"], m["file"])
    with open(SRC / "pdf" / x["pdf"], "rb") as f:
        r = c.post("/documents", files={"file": (x["file"], f, "application/pdf")}, data={"text": x["text"]},
                   headers=H[x["owner"]])
    x["id"] = r.json().get("id") if r.status_code == 201 else None
    if r.status_code != 201:
        print("übersprungen:", x["file"], r.status_code, r.text[:100])
for _ in range(600):
    left = [d for d in ok(c.get("/documents", params={"scope": "all", "limit": 2000}, headers=H["alex"])).json()
            if d.get("status") not in ("done", "error")]
    if not left:
        break
    time.sleep(1)

# --- Ordner per API (legt Tags richtig an), Metadaten direkt (inkl. § 35a, Rhythmus), alles verschoben auf heute
days = shift.delta_days(today)
for x, m in zip(docs, META):
    if x["id"] and m.get("tags"):
        ok(c.patch(f"/documents/{x['id']}", json={"tags": m["tags"]}, headers=H[x["owner"]]))
with sqlite3.connect(DB) as con:
    for x, m in zip(docs, META):
        if not x["id"]:
            continue
        vals = {k: m.get(k) for k in COLS}
        for k in ("document_date", "due_date"):
            vals[k] = shift.shift_date(vals[k], days)
        for k in ("title", "summary", "due_note"):
            vals[k] = shift.shift_text(vals[k], days)
        vals["due_done"] = vals["due_done"] or 0
        # Garantie, Ablaufdatum, Prüfung, Erstattung (für „Im Blick“) – nur wo meta.json sie nennt
        for k in ("expires_what", "service_what", "refund_state", "warranty_kind"):
            if m.get(k):
                vals[k] = m[k]
        for k in ("expires_on", "service_next"):
            if m.get(k):
                vals[k] = shift.shift_date(m[k], days)
        if "Rechnung" in (m.get("tags") or []) or m.get("doc_kind") == "rechnung":
            vals["warranty_months"] = m.get("warranty_months") or 0
            vals["warranty_until"] = warranty_end(vals["document_date"], vals["warranty_months"])
        sets = ", ".join(f"{k}=?" for k in vals)
        con.execute(f"UPDATE documents SET {sets}, llm_status='done', date_check=0, status='done' WHERE id=?",
                    (*vals.values(), x["id"]))

by_file = {x["file"]: x for x in docs if x["id"]}


def doc(name):
    return by_file[name]


def patch(name, who, **body):
    return ok(c.patch(f"/documents/{doc(name)['id']}", json=body, headers=H[who])).json()


# --- Familie: Kind (Regel auf den Vornamen), Notfallmappe, Fristen mit Zuständigen und Wiederholung
ok(c.post("/persons", json={"name": "Mia"}, headers=H["sam"]))
for x, m in zip(docs, META):   # Schul- und Kita-Post gehört zu Mia, auch wo ihr Name nicht im Text steht
    if x["id"] and {"Kinder", "Schule"} & set(m.get("tags", [])):
        ok(c.patch(f"/documents/{x['id']}", json={"tags": [*m["tags"], "Mia"]}, headers=H[x["owner"]]))
for name in ("HanseKraft_Beitragsrechnung_2026.pdf", "Haftpflicht_2026.pdf", "Darlehensvertrag_AveroBank.pdf"):
    if name in by_file:
        ok(c.post("/emergency/documents", json={"document_id": doc(name)["id"]}, headers=H[doc(name)["owner"]]))

iso = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731
if "Elternbrief_Klassenfahrt.pdf" in by_file:
    patch("Elternbrief_Klassenfahrt.pdf", "sam", due_assignee=ids["sam"])
if "Rechnung_Schornsteinfeger.pdf" in by_file:
    patch("Rechnung_Schornsteinfeger.pdf", "alex", due_assignee=ids["alex"])
if "Mahnung_Sanitaer_Becker.pdf" in by_file:   # einmal überfällig, damit man das Rot sieht
    patch("Mahnung_Sanitaer_Becker.pdf", "alex", due_date=iso(-2), due_done=False, due_note="Zahlung nach Mahnung",
          due_assignee=ids["alex"])
if "Rundfunkbeitrag_2025.pdf" in by_file:      # wiederkehrend
    patch("Rundfunkbeitrag_2025.pdf", "alex", due_date=iso(12), due_done=False, due_note="Rundfunkbeitrag",
          due_repeat="quarterly")
if "HanseKraft_Beitragsrechnung_2026.pdf" in by_file:  # Kündigungsfrist ohne Zuständige → „Noch niemand“
    patch("HanseKraft_Beitragsrechnung_2026.pdf", "alex", due_date=iso(24), due_done=False, due_note="Kündigungsfrist",
          due_repeat="yearly")

# --- Geld: Kontoumsätze aus zwei C24-Exporten (Alex: Gemeinschaftskonto, Pocket „Urlaub“, eigenes Sparkonto;
# Sam: Gemeinschaftskonto der letzten Monate und eigenes Sparkonto). Sparkonten bleiben privat, der Rest geteilt.
moved = [{"file": x["file"], "amount": m.get("amount"), "date": shift.shift_date(m.get("document_date"), days)}
         for x, m in zip(docs, META)]
for who, data in make_bank.exports(today, moved).items():
    other = ids["sam" if who == "alex" else "alex"]
    pre = ok(c.post("/bank/import", files={"file": (f"Umsaetze_{who}.csv", data, "text/csv")},
                    data={"preview": "true"}, headers=H[who])).json()
    shares = {a["key"]: not a["name"].startswith("Sparkonto") for a in pre.get("accounts") or []}
    assert shares, "Konten aus der Datei nicht erkannt"
    ok(c.post("/bank/import", files={"file": (f"Umsaetze_{who}.csv", data, "text/csv")},
              data={"preview": "false", "visible_to": str(other), "shares": json.dumps(shares)}, headers=H[who]))

# --- Prüfen: stimmt, was die App zeigen soll?
A = H["alex"]
rc = ok(c.get("/documents/review_count", params={"scope": "all"}, headers=A)).json()
dc = ok(c.get("/documents/due_count", params={"scope": "all"}, headers=A)).json()
persons = ok(c.get("/persons", headers=A)).json()
emerg = ok(c.get("/emergency", headers=A)).json()
owners = ok(c.get("/documents/owner_counts", headers=A)).json()
mia = next((p for p in persons if p["name"] == "Mia"), None)
problems = []
if not dc.get("count"):
    problems.append("keine offenen Fristen")
if not mia or (mia.get("count") or 0) < 2:
    problems.append("Mia ohne Dokumente")
if not emerg.get("documents"):
    problems.append("Notfallmappe leer")
if len(owners) < 2:
    problems.append("Dokumentzahlen fehlen")
if (rc.get("count") if isinstance(rc, dict) else rc) > 6:
    problems.append(f"zu viel unter „Zu prüfen“: {rc}")
wc = ok(c.get("/documents/warranty_count", params={"scope": "all"}, headers=A)).json()
up = {i["type"] for i in ok(c.get("/upcoming", params={"scope": "all"}, headers=A)).json()["items"]}
money = ok(c.get("/bank/summary", headers=A)).json()
accs = {who: [a["name"] for a in ok(c.get("/bank/accounts", headers=h)).json()["accounts"]] for who, h in H.items()}
if not wc.get("count"):
    problems.append("keine laufende Garantie")
if not {"expiry", "service", "refund", "price"} <= up:
    problems.append(f"„Im Blick“ unvollständig: {sorted(up)}")
if not money.get("count") or not 1 <= money.get("to_sort", 0) <= 15:
    problems.append(f"Geld: {money.get('count')} Umsätze, {money.get('to_sort')} zum Einsortieren")
if "Sparkonto Sam" in accs["alex"] or "Sparkonto Alex" in accs["sam"] or "Gemeinschaftskonto" not in accs["sam"]:
    problems.append(f"Kontosichtbarkeit falsch: {accs}")
print(f"Demo befüllt: {len(by_file)} Dokumente, Fristen {dc}, Zu prüfen {rc}, Personen {[p['name'] for p in persons]}, "
      f"Notfallmappe {len(emerg.get('documents', []))}, Garantien {wc.get('count')}, Im Blick {sorted(up)}, "
      f"Geld {money.get('count')} Umsätze ({money.get('to_sort')} einsortieren), Verschiebung {days} Tage")
if problems:
    sys.exit("Demo unvollständig: " + "; ".join(problems))
