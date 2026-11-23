#!/bin/sh
# Container start: make /data and /backup writable for the service user, then run the command without root.
#
# PUID/PGID (default 1000) choose the user the server runs as – set them to the owner of your bind-mounted
# folders (e.g. the user your scanner's FTP server writes as). Started with `--user`, the container skips
# all of this and runs the command directly.
set -eu

if [ "$(id -u)" != "0" ]; then
  exec tini -- "$@"
fi

PUID=${PUID:-1000}
PGID=${PGID:-1000}

# Keep the named user in sync so `docker compose exec -u papigator …` matches the running server
if [ "$(id -g papigator)" != "$PGID" ]; then groupmod -o -g "$PGID" papigator; fi
if [ "$(id -u papigator)" != "$PUID" ]; then usermod -o -u "$PUID" papigator; fi

fix_owner() {
  dir=$1
  [ -d "$dir" ] || mkdir -p "$dir" 2>/dev/null || { echo "papigator: cannot create $dir" >&2; return 0; }
  # Only touch what does not belong to the service user yet (fresh bind mount, files restored as root …)
  if ! find "$dir" \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown -h "$PUID:$PGID" {} + 2>/dev/null; then
    echo "papigator: could not change the owner of everything in $dir (network share?)." >&2
    echo "papigator: make sure UID $PUID / GID $PGID can write there, or set PUID/PGID." >&2
  fi
}

fix_owner "${PAPIGATOR_DATA:-/data}"
if [ -n "${PAPIGATOR_BACKUP:-}" ]; then fix_owner "$PAPIGATOR_BACKUP"; fi

export HOME="${PAPIGATOR_DATA:-/data}"
# tini (PID 1) forwards signals and reaps OCR subprocesses – it runs without root as well
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- tini -- "$@"
