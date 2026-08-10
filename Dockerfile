FROM python:3.12-slim

LABEL org.opencontainers.image.title="abs-m4b-tagger" \
      org.opencontainers.image.description="Ecrit les metadonnees Audiobookshelf dans les tags des fichiers m4b/mp3" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Paris

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

VOLUME ["/config"]

ENTRYPOINT ["python", "/app/main.py"]
