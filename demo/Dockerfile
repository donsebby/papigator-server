# Demoserver (App-Prüfung, Vorführung): normales Papigator-Image + Schrift und fpdf2 für die erfundenen Dokumente.
#   docker build -t papigator-server:local ..          # zuerst das normale Image (aus dem Server-Ordner)
#   docker compose up -d --build                       # dann dieses (siehe docker-compose.yml hier)
ARG BASE=papigator-server:local
FROM ${BASE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/* \
 && /venv/bin/pip install --no-cache-dir "fpdf2>=2.7,<3"
COPY demo /app/demo
RUN chmod 755 /app/demo/demo-run.sh
# Keine Sicherungen: die Demo wird täglich verworfen (auch die Befüllung läuft ohne PAPIGATOR_DEMO)
ENV DEMO_FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf \
    PAPIGATOR_DEMO=1 \
    PAPIGATOR_BACKUP=""
CMD ["/app/demo/demo-run.sh"]
