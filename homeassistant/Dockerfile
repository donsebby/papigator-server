# Home Assistant add-on: das Papigator-Image plus ein Startskript, das die Add-on-Optionen
# (/data/options.json) als PAPIGATOR_* weitergibt.
ARG BUILD_FROM=ghcr.io/donsebby/papigator:latest
FROM ${BUILD_FROM}

LABEL io.hass.type="addon" io.hass.name="Papigator"

COPY run.sh /usr/local/bin/papigator-addon
RUN chmod 755 /usr/local/bin/papigator-addon

ENTRYPOINT ["/usr/local/bin/papigator-addon"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
