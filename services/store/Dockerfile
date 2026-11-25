# Build from the repo root: docker build -f services/store/Dockerfile -t insidious/store:dev .
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY services/store/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
# Insidious SDK from this repo (with the [db] extra: SQLAlchemy + psycopg 3, also pinned in requirements.txt)
COPY sdk/pyproject.toml sdk/README.md sdk/LICENSE /opt/insidious-sdk/
COPY sdk/insidious /opt/insidious-sdk/insidious
RUN pip install --no-cache-dir "/opt/insidious-sdk[db]"
# Default config (a /config volume, e.g. a ConfigMap with insidious.yaml + flags.yaml, takes precedence)
COPY insidious.yaml /app/insidious.yaml
COPY flagsmith/flags.yaml /app/flagsmith/flags.yaml
COPY services/store/app /app/app
EXPOSE 8000
# One worker per pod on purpose: one pod saturates quickly, so scaling to 3 pods visibly helps.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
