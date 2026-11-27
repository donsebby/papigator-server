#!/usr/bin/env bash
# Prüft das Home-Assistant-Add-on (homeassistant/): baut es auf dem lokal gebauten Image auf, startet es so, wie der
# Supervisor es tut (/data mit options.json, /share eingebunden), dann:
#   ohne Token bricht es mit Hinweis ab → mit Token gesund → /ping fragt nach Einrichtung → erster Admin →
#   Datenbank in /data/papigator → ein Scan aus /share/papigator/inbox (von root abgelegt, wie per Samba) wird übernommen
#   → Sicherung nach /share/papigator/backup.
# Wo AppArmor läuft (GitHub-Runner, Linux mit sudo), laufen alle Starts unter homeassistant/apparmor.txt – wie beim
# Supervisor – und jede Verweigerung im Kernel-Log lässt die Prüfung scheitern. In CI ist AppArmor Pflicht.
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
grep -q "^## $VER\$" homeassistant/CHANGELOG.md || fail "CHANGELOG.md ohne Abschnitt „## $VER“ (HA zeigt ihn beim Update)"
# Der Supervisor liest genau ein „profile <name>“ am Zeilenanfang und setzt dort den Slug ein
[ "$(grep -c '^profile ' homeassistant/apparmor.txt)" = 1 ] || fail "apparmor.txt: genau ein Profil am Zeilenanfang nötig"

AA=()
PROFILE=papigator-addon-check
if [ "$(cat /sys/module/apparmor/parameters/enabled 2>/dev/null)" = Y ] && command -v apparmor_parser >/dev/null \
   && sudo -n true 2>/dev/null; then
  step "AppArmor-Profil laden (als $PROFILE)"
  sed "s/^profile [^ ]*/profile $PROFILE/" homeassistant/apparmor.txt > "$WORK/apparmor.txt"
  sudo apparmor_parser -r -W "$WORK/apparmor.txt" || fail "apparmor.txt lässt sich nicht laden"
  AA=(--security-opt "apparmor=$PROFILE")
  KLOG0=$(sudo dmesg | wc -l)
elif [ -n "${CI:-}" ]; then
  fail "kein AppArmor auf dem Runner – das Profil bliebe ungeprüft"
else
  echo "    (kein AppArmor hier – Profil ungeprüft)"
fi

step "Add-on bauen (auf $BASE)"
docker build -q --build-arg BUILD_FROM="$BASE" -t "$ADDON" homeassistant >/dev/null

mkdir -p "$WORK/data" "$WORK/share"
step "ohne Token: Abbruch mit Hinweis"
echo '{"setup_token": "", "secret_key": "", "inbox_dir": "/share/papigator/inbox", "backup_dir": "/share/papigator/backup", "llm_url": "", "llm_model": "gemma4:e4b"}' > "$WORK/data/options.json"
if docker run --rm ${AA[@]+"${AA[@]}"} -v "$WORK/data:/data" -v "$WORK/share:/share" "$ADDON" > "$WORK/out.txt" 2>&1; then fail "startet ohne Token"; fi
grep -q "setup_token" "$WORK/out.txt" || fail "kein Hinweis auf setup_token: $(cat "$WORK/out.txt")"

step "mit Token starten"
printf '{"setup_token": "%s", "secret_key": "", "inbox_dir": "/share/papigator/inbox", "backup_dir": "/share/papigator/backup", "llm_url": "", "llm_model": "gemma4:e4b", "language": "en"}' "$TOKEN" > "$WORK/data/options.json"
docker run -d ${AA[@]+"${AA[@]}"} --name "$NAME" -p "127.0.0.1:$PORT:8000" -v "$WORK/data:/data" -v "$WORK/share:/share" "$ADDON" >/dev/null
for i in $(seq 1 90); do
  h=$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo starting)
  [ "$h" = healthy ] && break
  [ "$i" = 90 ] && fail "nicht gesund nach 180 s ($h)"
  sleep 2
done

if [ ${#AA[@]} -gt 0 ]; then
  docker exec "$NAME" cat /proc/1/attr/current | grep -q "^$PROFILE (enforce)" || fail "läuft nicht unter $PROFILE"
fi

step "Einrichtung"
curl -fsS "$B/ping" | jq -e '.needs_setup == true' >/dev/null || fail "/ping fragt nicht nach Einrichtung"
curl -fsS -X POST "$B/users" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"username\":\"check\",\"display_name\":\"Check\",\"password\":\"$PW\"}" >/dev/null || fail "erster Nutzer"
AUTH="Authorization: Bearer $(curl -fsS -X POST "$B/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"check\",\"password\":\"$PW\"}" | jq -r .token)"
docker exec "$NAME" test -s /data/papigator/papigator.db || fail "keine Datenbank in /data/papigator"
docker exec "$NAME" test -d /share/papigator/backup || fail "Sicherungsordner fehlt"
curl -fsS "$B/settings" -H "$AUTH" | jq -e '.llm_language == "en"' >/dev/null || fail "Option language kommt nicht an"

step "zu allgemeiner Ordner (/share) wird abgelehnt"
printf '{"setup_token": "x", "secret_key": "", "inbox_dir": "/share/papigator/inbox", "backup_dir": "/share", "llm_url": "", "llm_model": ""}' > "$WORK/opt2.json"
mkdir -p "$WORK/data2" && cp "$WORK/opt2.json" "$WORK/data2/options.json"
if docker run --rm ${AA[@]+"${AA[@]}"} -v "$WORK/data2:/data" -v "$WORK/share:/share" "$ADDON" > "$WORK/out2.txt" 2>&1; then fail "startet mit backup_dir=/share"; fi
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

step "Sicherung nach /share/papigator/backup"
curl -fsS -X POST "$B/settings/backup/run" -H "$AUTH" >/dev/null || fail "Sicherung startet nicht"
for i in $(seq 1 60); do
  curl -fsS "$B/settings/backup" -H "$AUTH" | jq -e '(.running | not) and (.snapshots | length) > 0' >/dev/null && break
  [ "$i" = 60 ] && fail "keine Sicherung in /share/papigator/backup"
  sleep 2
done

step "Anhalten wie beim HA-Backup (backup: cold): TERM kommt an, sauberes Ende"
T0=$(date +%s)
docker stop -t 30 "$NAME" >/dev/null || fail "docker stop"
EXIT=$(docker inspect -f '{{.State.ExitCode}}' "$NAME")
[ $(( $(date +%s) - T0 )) -lt 25 ] || fail "Anhalten dauerte bis zum Abschuss"
case "$EXIT" in 0|143) ;; *) fail "Ende mit Code $EXIT (137 = abgeschossen, TERM kam nicht an)";; esac

if [ ${#AA[@]} -gt 0 ]; then
  step "AppArmor: keine Verweigerungen"
  DENIED=$(sudo dmesg | tail -n +"$((KLOG0 + 1))" | grep 'apparmor="DENIED"' | grep "profile=\"$PROFILE" || true)
  [ -z "$DENIED" ] || fail "AppArmor hat verweigert:
$DENIED"
fi

echo "OK: das Add-on startet wie unter Home Assistant (auch unter AppArmor), richtet sich ein, übernimmt Scans aus /share und sichert dorthin"
