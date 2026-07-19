# HearthMage container image.
#
# The app locates its templates and static files relative to the package
# directory, so it must run from a real source tree. We install the package
# editable from the copied source, which leaves those files in place while
# still resolving the declared dependencies.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HEARTHMAGE_CONFIG_FILE=/data/config.json

WORKDIR /app

# Install first with just the metadata + source needed, for layer caching.
COPY pyproject.toml ./
COPY src ./src
COPY run.py ./
RUN pip install --no-cache-dir -e ".[mqtt]"

# Run as an unprivileged user; /data holds config, schedules, history, backups.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin hearth \
    && mkdir -p /data \
    && chown hearth:hearth /data
USER hearth

VOLUME ["/data"]

# Host networking is recommended (hub discovery needs the LAN broadcast
# domain), so this EXPOSE is informational. Default listen port is 8080.
EXPOSE 8080

# Poll the app's own health endpoint. Uses the default port; override the
# healthcheck if you change HEARTHMAGE_PORT.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"

CMD ["python", "run.py"]
