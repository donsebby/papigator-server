"""Nur die öffentlichen Freigabe-Seiten (/s/…) als eigene App – zum Betrieb auf einem eigenen Port.

So lässt sich Papigator teilen, ohne die eigentliche Anwendung ins Internet zu stellen (ähnlich dem
„Public Proxy“ von Immich): Der Reverse-Proxy reicht nur diesen Port weiter, die Firewall lässt nur ihn
durch. Hier gibt es keine Anmeldung, keine API und keine Web-App – nur Freigabeseite, PDF, Vorschaubild
und Passwortabfrage eines gültigen Links. Datenbank, Daten und Schlüssel sind dieselben wie beim
Hauptdienst (gleiche Env-Datei); Migrationen und Worker startet nur der Hauptdienst.

    uvicorn app.share_app:app --host 0.0.0.0 --port 8001
"""
from fastapi import FastAPI

from . import share

app = FastAPI(title="Papigator Freigaben", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(share.public)


@app.get("/s/_/ping", include_in_schema=False)
def ping():
    """Lebenszeichen für Reverse-Proxy und Überwachung – verrät nichts über Links oder Dokumente."""
    return {"ok": True}
