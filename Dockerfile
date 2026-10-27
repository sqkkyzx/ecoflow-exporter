FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EXPORTER_LISTEN=0.0.0.0 \
    EXPORTER_PORT=9098

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 exporter \
    && useradd --uid 10001 --gid exporter --no-create-home --shell /usr/sbin/nologin exporter

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY --chown=10001:10001 exporter.py protocol.py recovery.py metrics.py ./
COPY --chown=10001:10001 LICENSE NOTICE ./
COPY --chown=10001:10001 vendor/ ./vendor/

USER 10001:10001
EXPOSE 9098
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:'+os.environ.get('EXPORTER_PORT','9098')+'/healthz',timeout=2).read()"]
ENTRYPOINT ["python", "-u", "exporter.py"]
