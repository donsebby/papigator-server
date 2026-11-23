#!/bin/sh
# Demoserver-Schleife: Daten löschen → erfundenen Haushalt mit heutigen Daten erzeugen → über einen nur lokal
# lauschenden Server befüllen (ohne Demo-Sperren, mit Einmal-Token) → öffentlich mit PAPIGATOR_DEMO=1 starten →
# täglich um DEMO_RESET_HOUR (Ortszeit, TZ) von vorn. Schlägt das Befüllen fehl, geht nichts öffentlich und es
# wird nach fünf Minuten neu versucht.
#   Pflicht: PAPIGATOR_SECRET_KEY, DEMO_PASSWORD (für alex und sam)
set -u
DATA=${PAPIGATOR_DATA:-/data}
HOUR=${DEMO_RESET_HOUR:-11}
: "${DEMO_PASSWORD:?DEMO_PASSWORD fehlt}"
cd /app

seconds_until_reset() {
  python - "$HOUR" <<'PY'
import sys, datetime as dt
h = int(sys.argv[1]); now = dt.datetime.now()
nxt = now.replace(hour=h, minute=0, second=0, microsecond=0)
if nxt <= now: nxt += dt.timedelta(days=1)
print(int((nxt - now).total_seconds()))
PY
}

wait_ping() {
  i=0
  while [ $i -lt 120 ]; do
    python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ping', timeout=2)" 2>/dev/null && return 0
    i=$((i + 1)); sleep 1
  done
  return 1
}

# Beenden mit Frist: erst freundlich, nach 20 s hart – ein hängender Download hielte das Zurücksetzen sonst auf
stop() {
  kill "$1" 2>/dev/null
  i=0
  while kill -0 "$1" 2>/dev/null && [ $i -lt 20 ]; do sleep 1; i=$((i + 1)); done
  kill -9 "$1" 2>/dev/null
  wait "$1" 2>/dev/null
}

seed() {
  find "$DATA" -mindepth 1 -delete 2>/dev/null
  rm -rf /tmp/demo-docs
  DEMO_SHIFT=1 python demo/make_docs.py /tmp/demo-docs || return 1
  boot=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
  # Befüllen ohne Sprachmodell (Metadaten kommen aus meta.json) und ohne Demo-Sperren, nur auf 127.0.0.1
  env -u PAPIGATOR_LLM_URL PAPIGATOR_DEMO=0 PAPIGATOR_TOKEN="$boot" \
    uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-server-header --log-level warning --timeout-graceful-shutdown 10 &
  pid=$!
  if wait_ping && PAPIGATOR_URL=http://127.0.0.1:8000 PAPIGATOR_TOKEN="$boot" python demo/seed.py /tmp/demo-docs; then
    rc=0
  else
    rc=1
  fi
  stop "$pid"
  rm -rf /tmp/demo-docs
  return $rc
}

while true; do
  echo "demo: neu befüllen ($(date))"
  if ! seed; then
    echo "demo: Befüllen fehlgeschlagen – neuer Versuch in 5 Minuten" >&2
    sleep 300
    continue
  fi
  PAPIGATOR_DEMO=1 PAPIGATOR_TOKEN="" uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-server-header \
    --timeout-graceful-shutdown 10 &
  pid=$!
  started=$(date +%s)
  s=$(seconds_until_reset)
  echo "demo: öffentlich, nächstes Zurücksetzen in $((s / 3600)) h"
  sleep "$s" & spid=$!
  # endet der Server vorher (Absturz), sofort neu befüllen
  while kill -0 "$pid" 2>/dev/null && kill -0 "$spid" 2>/dev/null; do sleep 5; done
  kill "$spid" 2>/dev/null
  stop "$pid"
  # stürzt der Server gleich nach dem Start wieder ab, nicht im Kreis befüllen: erst eine Pause
  if [ $(( $(date +%s) - started )) -lt 300 ]; then
    echo "demo: Server nach kurzer Zeit beendet – Pause vor dem Neubefüllen" >&2
    sleep 120
  fi
done
