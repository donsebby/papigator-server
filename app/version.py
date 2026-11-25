"""Server-Version – eine Stelle für alles: /health, die Update-Meldung (app/updates.py), das Home-Assistant-Add-on
(homeassistant/config.yaml `version` und der Image-Tag in build.yaml müssen gleich sein; ci/ha-addon-check.sh prüft das)
und das GitHub-Release, das tools/publish-server.sh beim Veröffentlichen anlegt (Tag v<VERSION>).
Für jeden neuen öffentlichen Server-Stand erhöhen: Datum, bei mehreren am Tag .1, .2 …"""
VERSION = "2026.11.25.1"
