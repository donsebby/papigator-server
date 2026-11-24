#!/usr/bin/env bash
# Checks the Unraid template (templates/papigator.xml) against the image: starts ONE container exactly with the
# template's ports, paths, variables and extra parameters (the way Unraid's Docker manager would, required
# fields filled in, optional empty ones left out, PUID/PGID 99/100), then:
#   healthy → /ping asks for setup → first admin via the setup token → files belong to 99:100 →
#   a scan dropped into the mapped scanner share is imported and OCR'd.
#
#   PAPIGATOR_IMAGE=papigator-server:local ci/unraid-template-check.sh
#
# Needs docker, python3, curl and jq. Leaves nothing running.
set -euo pipefail

cd "$(dirname "$0")/.."
IMAGE=${PAPIGATOR_IMAGE:-papigator-server:local}
TEMPLATE=templates/papigator.xml
NAME=papigator-unraid-check
PORT=${UNRAID_CHECK_PORT:-18100}
B="http://127.0.0.1:$PORT"
WORK=$(mktemp -d)
PW="check-$(openssl rand -hex 8)"

fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo "--- $*"; }

cleanup() {
  status=$?
  if [ $status -ne 0 ]; then
    echo "::group::container logs"; docker logs --tail 200 "$NAME" 2>&1 || true; echo "::endgroup::"
  fi
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run --rm -v "$WORK:/w" --entrypoint sh "$IMAGE" -c 'rm -rf /w/*' >/dev/null 2>&1 || true
  rm -rf "$WORK"
  exit $status
}
trap cleanup EXIT

step "read the template"
# One docker-run argument per line; host folders for paths under $WORK, the optional scanner share included
python3 - "$TEMPLATE" "$WORK" "$PORT" > "$WORK/args" <<'PY'
import re, secrets, shlex, sys
import xml.etree.ElementTree as ET

tpl, work, port = sys.argv[1:]
root = ET.parse(tpl).getroot()
assert root.tag == "Container" and root.get("version") == "2", "not a v2 container template"
for tag in ("Name", "Repository", "Overview", "Category", "WebUI", "TemplateURL", "Icon", "Support", "Project"):
    assert (root.findtext(tag) or "").strip(), f"<{tag}> missing"
assert root.findtext("TemplateURL").endswith("/" + tpl), "TemplateURL does not point to this file"
names = [c.get("Name") for c in root.iter("Config")]
assert len(names) == len(set(names)), "duplicate Config names"

args = []
for c in root.iter("Config"):
    kind, target = c.get("Type"), c.get("Target")
    value = (c.text or c.get("Default") or "").strip()
    if kind == "Port":
        args += ["-p", f"127.0.0.1:{port}:{target}/{c.get('Mode') or 'tcp'}"]
    elif kind == "Path":
        host = f"{work}/{re.sub(r'[^a-z0-9]+', '-', c.get('Name').lower()).strip('-')}"
        args += ["-v", f"{host}:{target}:{c.get('Mode') or 'rw'}"]
    elif kind == "Variable":
        if not value and c.get("Required") == "true":
            value = secrets.token_hex(24)   # what the user types in
        if value:
            args += ["-e", f"{target}={value}"]
args += shlex.split(root.findtext("ExtraParams") or "")
print("\n".join(args))
PY
mapfile -t ARGS < "$WORK/args"
TOKEN=$(sed -n 's/^PAPIGATOR_TOKEN=//p' "$WORK/args")
[ -n "$TOKEN" ] || fail "template has no setup token"
grep -q '^PAPIGATOR_SECRET_KEY=' "$WORK/args" && fail "secret key should be optional (empty by default)"
INBOX="$WORK/scanner-inbox-optional"
mkdir -p "$INBOX" && chmod 777 "$INBOX"    # an Unraid share: writable for the scanner

step "start like Unraid would"
docker run -d --name "$NAME" "${ARGS[@]}" "$IMAGE" >/dev/null

for i in $(seq 1 90); do
  h=$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo starting)
  [ "$h" = healthy ] && break
  [ "$i" = 90 ] && fail "not healthy after 180 s ($h)"
  sleep 2
done

step "web UI and first-run setup"
WEBUI=$(python3 -c "import sys,xml.etree.ElementTree as E; print(E.parse(sys.argv[1]).getroot().findtext('WebUI'))" "$TEMPLATE" \
        | sed "s|\[IP\]|127.0.0.1|; s|\[PORT:[0-9]*\]|$PORT|")
curl -fsS "$WEBUI/" | grep -q 'app.js' || fail "web UI $WEBUI"
curl -fsS "$B/ping" | jq -e '.needs_setup == true' >/dev/null || fail "/ping does not ask for setup"
curl -fsS -X POST "$B/users" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"username\":\"check\",\"display_name\":\"Check\",\"password\":\"$PW\"}" >/dev/null || fail "creating the first user"
AUTH="Authorization: Bearer $(curl -fsS -X POST "$B/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"check\",\"password\":\"$PW\"}" | jq -r .token)"

step "files belong to 99:100 (nobody:users)"
[ "$(docker exec "$NAME" stat -c '%u:%g' /data/papigator.db)" = "99:100" ] || fail "database does not belong to 99:100"
# (without PAPIGATOR_SECRET_KEY the key file is created on first use, e.g. when a mail password is saved)

step "scan into the mapped scanner share → imported with OCR"
docker exec -i "$NAME" python - > "$WORK/scan.pdf" <<'PY'
import io, sys
from PIL import Image, ImageDraw, ImageFont
img = Image.new("L", (1240, 1754), 255)
d = ImageDraw.Draw(img)
font = ImageFont.load_default(size=56)
for i, line in enumerate(["Rechnung Nummer 4711", "Unraidtest Papigator"]):
    d.text((120, 200 + i * 110), line, fill=0, font=font)
buf = io.BytesIO()
img.save(buf, "PDF", resolution=150)
sys.stdout.buffer.write(buf.getvalue())
PY
cp "$WORK/scan.pdf" "$INBOX/scan.pdf"
for i in $(seq 1 120); do
  ID=$(curl -fsS "$B/documents?q=Unraidtest" -H "$AUTH" | jq -r '.[0].id // empty')
  [ -n "$ID" ] && break
  [ "$i" = 120 ] && fail "scan from the scanner share not imported after 240 s"
  sleep 2
done
[ ! -e "$INBOX/scan.pdf" ] || fail "scan was not removed from the scanner share"

echo "OK: the Unraid template starts the image, sets up, and imports from the scanner share"
