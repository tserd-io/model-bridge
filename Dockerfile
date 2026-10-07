FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IDEMPOTENCY_DB_PATH=/data/gateway_requests.sqlite3

WORKDIR /app

# Install locked application dependencies before copying source.
COPY requirements-ci.lock ./
RUN python -m pip install --no-cache-dir --require-hashes \
        -r requirements-ci.lock \
    && python -m pip check

# Give the application a dedicated user and writable storage directory.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir /data \
    && chown app:app /data

COPY model_bridge/ ./model_bridge/

USER app

EXPOSE 8000

# Check database readiness without requiring curl in the image.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health/ready', timeout=2).close()"]

CMD ["python", "-m", "model_bridge.main"]
