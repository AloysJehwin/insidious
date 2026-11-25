# Build from the repo root: docker build -f services/agent/Dockerfile -t insidious/agent:dev .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    INSIDIOUS_REPORTS_DIR=/app/reports
WORKDIR /app

COPY services/agent/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/agent/app /app/app
# Fallback config; in k8s the real ones are mounted at /config (INSIDIOUS_CONFIG / INSIDIOUS_FLAGS).
COPY insidious.yaml /app/insidious.yaml
COPY flagsmith/flags.yaml /app/flags.yaml
RUN mkdir -p /data /app/reports && useradd -u 10001 -m agent && chown -R agent /app /data
USER 10001

EXPOSE 8004
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004"]
