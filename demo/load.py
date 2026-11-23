"""Frische Instanz mit Demo-Daten füllen: legt Alex (Admin) und Sam an, gibt gegenseitig frei (Partner mit
Schreibrecht) und lädt die Dokumente aus make_docs.py samt Text hoch (der Server macht dann kein OCR).

    PAPIGATOR_URL=http://127.0.0.1:8765 PAPIGATOR_TOKEN=<Bootstrap-Token> DEMO_PASSWORD=<Passwort> \
        python load.py /tmp/papigator-demo

Die Anmelde-Tokens landen in <Ausgabe>/tokens.json (nur für Prüfskripte, nicht weitergeben).
"""
import json
import os
import sys
from pathlib import Path

import httpx

B = os.environ.get("PAPIGATOR_URL", "http://127.0.0.1:8765")
H = Path(sys.argv[1] if len(sys.argv) > 1 else "papigator-demo")
pw = os.environ["DEMO_PASSWORD"]
c = httpx.Client(base_url=B, timeout=60)
r = c.post("/users", json={"username": "alex", "display_name": "Alex", "password": pw},
           headers={"Authorization": f"Bearer {os.environ['PAPIGATOR_TOKEN']}"})
r.raise_for_status()


def login(u):
    t = c.post("/auth/login", json={"username": u, "password": pw, "label": "Test"}).json()["token"]
    c.cookies.clear()
    return {"Authorization": f"Bearer {t}"}


A = login("alex")
c.post("/users", json={"username": "sam", "display_name": "Sam", "password": pw}, headers=A).raise_for_status()
S = login("sam")
me_a = c.get("/auth/me", headers=A).json(); me_s = c.get("/auth/me", headers=S).json()
c.put("/shares", json={"user_id": me_s["id"], "can_edit": True}, headers=A).raise_for_status()
c.put("/shares", json={"user_id": me_a["id"], "can_edit": True}, headers=S).raise_for_status()

docs = json.load(open(H / "docs.json"))
for x in docs:
    hdr = A if x["owner"] == "alex" else S
    with open(H / "pdf" / x["pdf"], "rb") as f:
        r = c.post("/documents", files={"file": (x["file"], f, "application/pdf")}, data={"text": x["text"]}, headers=hdr)
    x["id"] = r.json().get("id") if r.status_code == 201 else None
    x["upload_status"] = r.status_code
    if r.status_code != 201:
        print(x["file"], r.status_code, r.text[:120])
json.dump(docs, open(H / "docs.json", "w"), ensure_ascii=False, indent=1)
json.dump({"alex": A, "sam": S}, open(H / "tokens.json", "w"))
print("hochgeladen:", sum(1 for x in docs if x["id"]))
