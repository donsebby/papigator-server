#!/usr/bin/env bash
# Smoke test for the Docker image: start it the way the README says (docker compose with a fresh .env and empty
# data folders), then check what a new self-hoster would do first:
#   both services healthy → /ping → first-run setup (first admin via the bootstrap token) → login →
#   upload a scanned PDF without a text layer → OCR in the image finds the text → thumbnail → web app → share pages.
#
#   (im Server-Ordner) PAPIGATOR_IMAGE=papigator-server:local ci/docker-smoke.sh
#
# Needs docker (with compose), curl and jq. Leaves nothing running; on failure it prints the container logs.
set -euo pipefail

cd "$(dirname "$0")/.."
IMAGE=${PAPIGATOR_IMAGE:-papigator-server:local}
WORK=$(mktemp -d)
PORT=${SMOKE_PORT:-18000}
SHARE_PORT=${SMOKE_SHARE_PORT:-18001}
B="http://127.0.0.1:$PORT"
PW="smoke-$(openssl rand -hex 8)"

cp docker-compose.yml "$WORK/"
cat > "$WORK/.env" <<EOF
PAPIGATOR_IMAGE=$IMAGE
PAPIGATOR_SECRET_KEY=$(openssl rand -base64 32)
PAPIGATOR_TOKEN=$(openssl rand -hex 24)
PAPIGATOR_PORT=$PORT
PAPIGATOR_SHARE_PORT=$SHARE_PORT
EOF
TOKEN=$(sed -n 's/^PAPIGATOR_TOKEN=//p' "$WORK/.env")
compose() { docker compose --project-directory "$WORK" -f "$WORK/docker-compose.yml" -p papigator-smoke "$@"; }

cleanup() {
  status=$?
  if [ $status -ne 0 ]; then
    echo "::group::container logs"; compose logs --no-color --tail 200 || true; echo "::endgroup::"
  fi
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  # the data folders belong to the container user; remove them from inside a container
  docker run --rm -v "$WORK:/w" --entrypoint sh "$IMAGE" -c 'rm -rf /w/data /w/backup' >/dev/null 2>&1 || true
  rm -rf "$WORK"
  exit $status
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo "--- $*"; }

step "start (docker compose up, no build)"
compose up -d --no-build --quiet-pull

step "wait until both services are healthy"
for i in $(seq 1 90); do
  main=$(docker inspect -f '{{.State.Health.Status}}' papigator 2>/dev/null || echo starting)
  share=$(docker inspect -f '{{.State.Health.Status}}' papigator-share 2>/dev/null || echo starting)
  [ "$main" = healthy ] && [ "$share" = healthy ] && break
  [ "$i" = 90 ] && fail "not healthy after 180 s (main: $main, share: $share)"
  sleep 2
done

step "/ping"
curl -fsS "$B/ping" | jq -e '.ok == true and .needs_setup == true' >/dev/null || fail "/ping does not ask for setup"

step "first-run setup: first user becomes admin"
curl -fsS -X POST "$B/users" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"username\":\"smoke\",\"display_name\":\"Smoke\",\"password\":\"$PW\"}" >/dev/null || fail "creating the first user"
curl -fsS "$B/ping" | jq -e '.needs_setup == false' >/dev/null || fail "setup still pending"
AUTH="Authorization: Bearer $(curl -fsS -X POST "$B/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"smoke\",\"password\":\"$PW\"}" | jq -r .token)"
curl -fsS "$B/auth/me" -H "$AUTH" | jq -e '.is_admin == true' >/dev/null || fail "first user is not admin"

step "make a scanned PDF (image only, no text layer) inside the image"
compose exec -T papigator python - <<'PY' > "$WORK/scan.pdf"
import io, sys
from PIL import Image, ImageDraw, ImageFont
img = Image.new("L", (1240, 1754), 255)
d = ImageDraw.Draw(img)
font = ImageFont.load_default(size=56)
for i, line in enumerate(["Rechnung Nummer 4711", "Rauchtest Papigator", "Gesamtbetrag 123,45 EUR"]):
    d.text((120, 200 + i * 110), line, fill=0, font=font)
buf = io.BytesIO()
img.save(buf, "PDF", resolution=150)
sys.stdout.buffer.write(buf.getvalue())
PY
[ -s "$WORK/scan.pdf" ] || fail "no PDF generated"

step "upload and wait for OCR"
ID=$(curl -fsS -X POST "$B/documents" -H "$AUTH" -F "file=@$WORK/scan.pdf;type=application/pdf" | jq -r .id)
[ "$ID" != null ] || fail "upload returned no id"
for i in $(seq 1 90); do
  st=$(curl -fsS "$B/documents/$ID" -H "$AUTH" | jq -r .status)
  [ "$st" = done ] && break
  [ "$st" = error ] && fail "processing ended with status error"
  [ "$i" = 90 ] && fail "OCR not finished after 180 s (status $st)"
  sleep 2
done
TEXT=$(curl -fsS "$B/documents/$ID?include_text=true" -H "$AUTH" | jq -r .text)
echo "$TEXT" | grep -qi "rauchtest" || fail "OCR text does not contain 'Rauchtest': $(echo "$TEXT" | head -c 300)"
curl -fsS "$B/documents?q=Rauchtest" -H "$AUTH" | jq -e "map(.id) | index($ID) != null" >/dev/null || fail "full-text search"

step "thumbnail, file, web app, share service"
curl -fsS "$B/documents/$ID/thumbnail" -H "$AUTH" -o "$WORK/thumb" && [ -s "$WORK/thumb" ] || fail "thumbnail"
curl -fsS "$B/documents/$ID/file" -H "$AUTH" -o "$WORK/file.pdf" && head -c 5 "$WORK/file.pdf" | grep -q '%PDF' || fail "file"
curl -fsS "$B/app/" | grep -q 'app.js' || fail "web app"
curl -fsS "http://127.0.0.1:$SHARE_PORT/s/_/ping" >/dev/null || fail "share service"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$SHARE_PORT/documents")
[ "$code" = 404 ] || fail "share service answers outside /s/ ($code)"

echo "OK: image starts, sets up, runs OCR and serves the web app"
