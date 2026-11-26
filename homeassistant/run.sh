#!/bin/sh
# Start im Home-Assistant-Add-on: Optionen aus /data/options.json → PAPIGATOR_*, dann der normale Start des Images.
#
# - Daten in /data/papigator (Add-on-Speicher, landet in den HA-Backups); /data/options.json gehört HA.
# - Scanner-Eingang unter /share/papigator/inbox – über das Samba-Add-on erreichbar. Papigators eigene Sicherung ist
#   standardmäßig aus: die Home-Assistant-Backups enthalten die Add-on-Daten (verschlüsselt, angehalten – backup: cold);
#   eine Kopie unter /share läge unverschlüsselt für Samba und andere Add-ons lesbar.
# - Läuft als root (PUID/PGID 0): /share und vom Samba-Add-on angelegte Ordner gehören root; so kann Papigator
#   eingeworfene Scans lesen und danach löschen.
set -eu

OPTS=/data/options.json
opt() {
  python -c 'import json, sys
try:
    v = json.load(open(sys.argv[1])).get(sys.argv[2])
except Exception:
    v = None
print("" if v is None else v)' "$OPTS" "$1"
}

PAPIGATOR_DATA=/data/papigator
PAPIGATOR_TOKEN=$(opt setup_token)
PAPIGATOR_INBOX=$(opt inbox_dir)
PAPIGATOR_BACKUP=$(opt backup_dir)
export PAPIGATOR_DATA PAPIGATOR_TOKEN PAPIGATOR_INBOX PAPIGATOR_BACKUP
key=$(opt secret_key);  [ -n "$key" ] && export PAPIGATOR_SECRET_KEY="$key"
llm=$(opt llm_url);     [ -n "$llm" ] && export PAPIGATOR_LLM_URL="$llm"
model=$(opt llm_model); [ -n "$model" ] && export PAPIGATOR_LLM_MODEL="$model"
lang=$(opt language);    [ -n "$lang" ] && export PAPIGATOR_LANGUAGE="$lang"
export PUID=0 PGID=0

# Nur eigene Unterordner: das Start-Skript des Images macht alles darin zum Eigentum des Dienstes (hier root) –
# auf /share selbst angewandt träfe das jede Datei der anderen Add-ons
for d in "$PAPIGATOR_INBOX" "$PAPIGATOR_BACKUP"; do
  [ -n "$d" ] || continue
  case "${d%/}" in
    ""|/share|/data|/config|/ssl|/media|/addons|/backup)
      echo "papigator: $d ist zu allgemein – bitte einen eigenen Unterordner wählen, z. B. /share/papigator/… / use a subfolder." >&2
      exit 1;;
  esac
done

if [ -z "$PAPIGATOR_TOKEN" ]; then
  echo "papigator: setup_token fehlt / is missing – set it in the add-on configuration (any long random string)." >&2
  exit 1
fi
mkdir -p "$PAPIGATOR_DATA" "$PAPIGATOR_INBOX"
[ -n "$PAPIGATOR_BACKUP" ] && mkdir -p "$PAPIGATOR_BACKUP"

exec docker-entrypoint.sh "$@"
