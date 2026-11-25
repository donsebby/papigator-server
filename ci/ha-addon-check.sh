#!/usr/bin/env bash
# Prüft das Home-Assistant-Add-on (homeassistant/): baut es auf dem lokal gebauten Image auf, startet es so, wie der
# Supervisor es tut (/data mit options.json, /share eingebunden), dann:
#   ohne Token bricht es mit Hinweis ab → mit Token gesund → /ping fragt nach Einrichtung → erster Admin →
#   Datenbank in /data/papigator → ein Scan aus /share/papigator/inbox (von root abgelegt, wie per Samba) wird übernommen.
#
#   PAPIGATOR_IMAGE=papigator-server:local ci/ha-addon-check.sh
set -euo pipefail

cd "$(dirname "$0")/.."
BASE=${PAPIGATOR_IMAGE:-papigator-server:local}
ADDON=papigator-addon:check
NAME=papigator-addon-check
PORT=${HA_CHECK_PORT:-18200}
B="http://127.0.0.1:$PORT"
WORK=$(mktemp -d)
PW="check-$(openssl rand -hex 8)"
TOKEN=$(openssl rand -hex 24)

fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo "--- $*"; }
cleanup() {
  status=$?
  if [ $status -ne 0 ]; then echo "::group::container logs"; docker logs --tail 200 "$NAME" 2>&1 || true; echo "::endgroup::"; fi
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run --rm -v "$WORK:/w" --entrypoint sh "$BASE" -c 'rm -rf /w/*' >/dev/null 2>&1 || true
  rm -rf "$WORK"
  exit $status
}
trap cleanup EXIT

step "config.yaml: Pflichtfelder"
for k in "name: Papigator" "slug: papigator" "version:" "aarch64" "amd64" "8000/tcp: 8000" "share:rw" "setup_token: password"; do
  grep -q -- "$k" homeassistant/config.yaml || fail "config.yaml ohne „$k“"
done
grep -q "build_from" homeassistant/build.yaml || fail "build.yaml ohne build_from"
VER=$(sed -n 's/^version: *"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' homeassistant/config.yaml)
[ -n "$VER" ] || fail "config.yaml: version nicht lesbar"
APPVER=$(sed -n 's/^VERSION = "\([^"]*\)"$/\1/p' app/version.py)
[ "$VER" = "$APPVER" ] || fail "config.yaml version ($VER) ≠ app/version.py ($APPVER)"
for a in aarch64 amd64; do
  grep -q "^  $a: ghcr.io/donsebby/papigator:$VER\$" homeassistant/build.yaml || fail "build.yaml: $a nicht auf :$VER (version aus config.yaml)"
done
grep -q "^backup: cold" homeassistant/config.yaml || fail "config.yaml ohne backup: cold"
[ -f repository.yaml ] || fail "repository.yaml fehlt im Repo-Wurzelordner"

step "Add-on bauen (auf $BASE)"
docker build -q --build-arg BUILD_FROM="$BASE" -t "$ADDON" homeassistant >/dev/null

mkdir -p "$WORK/data" "$WORK/share"
step "ohne Token: Abbruch mit Hinweis"
echo '{"setup_token": "", "secret_key": "", "inbox_dir": "/share/papigator/inbox", "backup_dir": "/share/papigator/backup", "llm_url": "", "llm_model": "gemma4:e4b"}' > "$WORK/data/options.json"
if docker run --rm -v "$WORK/data:/data" -v "$WORK/share:/share" "$ADDON" > "$WORK/out.txt" 2>&1; then fail "startet ohne Token"; fi
grep -q "setup_token" "$WORK/out.txt" || fail "kein Hinweis auf setup_token: $(cat "$WORK/out.txt")"

step "mit Token starten"
printf '{"setup_token": "%s", "secret_key": "", "inbox_dir": "/share/papigator/inbox", "backup_dir": "/share/papigator/backup", "llm_url": "", "llm_model": "gemma4:e4b"}' "$TOKEN" > "$WORK/data/options.json"
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8000" -v "$WORK/data:/data" -v "$WORK/share:/share" "$ADDON" >/dev/null
for i in $(seq 1 90); do
  h=$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo starting)
  [ "$h" = healthy ] && break
  [ "$i" = 90 ] && fail "nicht gesund nach 180 s ($h)"
  sleep 2
done

step "Einrichtung"
curl -fsS "$B/ping" | jq -e '.needs_setup == true' >/dev/null || fail "/ping fragt nicht nach Einrichtung"
curl -fsS -X POST "$B/users" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"username\":\"check\",\"display_name\":\"Check\",\"password\":\"$PW\"}" >/dev/null || fail "erster Nutzer"
AUTH="Authorization: Bearer $(curl -fsS -X POST "$B/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"check\",\"password\":\"$PW\"}" | jq -r .token)"
docker exec "$NAME" test -s /data/papigator/papigator.db || fail "keine Datenbank in /data/papigator"
docker exec "$NAME" test -d /share/papigator/backup || fail "Sicherungsordner fehlt"

step "zu allgemeiner Ordner (/share) wird abgelehnt"
printf '{"setup_token": "x", "secret_key": "", "inbox_dir": "/share/papigator/inbox", "backup_dir": "/share", "llm_url": "", "llm_model": ""}' > "$WORK/opt2.json"
mkdir -p "$WORK/data2" && cp "$WORK/opt2.json" "$WORK/data2/options.json"
if docker run --rm -v "$WORK/data2:/data" -v "$WORK/share:/share" "$ADDON" > "$WORK/out2.txt" 2>&1; then fail "startet mit backup_dir=/share"; fi
grep -q "zu allgemein" "$WORK/out2.txt" || fail "kein Hinweis bei /share: $(cat "$WORK/out2.txt")"

step "Scan über /share/papigator/inbox (als root abgelegt, wie per Samba)"
docker exec -i "$NAME" python - > "$WORK/scan.pdf" <<'PY'
import io, sys
from PIL import Image, ImageDraw, ImageFont
img = Image.new("L", (1240, 1754), 255)
d = ImageDraw.Draw(img)
font = ImageFont.load_default(size=56)
for i, line in enumerate(["Rechnung Nummer 4711", "Addontest Papigator"]):
    d.text((120, 200 + i * 110), line, fill=0, font=font)
buf = io.BytesIO(); img.save(buf, "PDF", resolution=150); sys.stdout.buffer.write(buf.getvalue())
PY
docker cp "$WORK/scan.pdf" "$NAME:/share/papigator/inbox/scan.pdf"
for i in $(seq 1 120); do
  ID=$(curl -fsS "$B/documents?q=Addontest" -H "$AUTH" | jq -r '.[0].id // empty')
  [ -n "$ID" ] && break
  [ "$i" = 120 ] && fail "Scan aus /share/papigator/inbox nicht übernommen"
  sleep 2
done
docker exec "$NAME" test ! -e /share/papigator/inbox/scan.pdf || fail "Scan nicht aus dem Eingang entfernt"

echo "OK: das Add-on startet wie unter Home Assistant, richtet sich ein und übernimmt Scans aus /share"
