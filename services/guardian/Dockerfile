# Build from the repo root: docker build -f services/guardian/Dockerfile -t insidious/guardian:dev .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    INSIDIOUS_REPORTS_DIR=/app/reports
WORKDIR /app

COPY services/guardian/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY services/guardian/app /app/app
# Fallback config; in k8s mount the real one at /config/insidious.yaml (INSIDIOUS_CONFIG).
COPY insidious.yaml /app/insidious.yaml
RUN mkdir -p /app/reports && useradd -u 10001 -m guardian && chown -R guardian /app
USER 10001

EXPOSE 8002
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
