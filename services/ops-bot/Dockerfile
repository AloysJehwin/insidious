# Build from the repo root: docker build -f services/ops-bot/Dockerfile -t insidious/ops-bot:dev .
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY services/ops-bot/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/ops-bot/app ./app

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data
RUN useradd --create-home --uid 10001 opsbot && mkdir -p /data && chown opsbot /data
USER opsbot

EXPOSE 8003
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003", "--proxy-headers"]
