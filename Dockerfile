FROM python:3.12-slim

LABEL org.opencontainers.image.title="abs-m4b-tagger" \
      org.opencontainers.image.description="Ecrit les metadonnees Audiobookshelf dans les tags des fichiers m4b/mp3" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Paris

# ffmpeg + ffprobe : requis par SYNC_CHAPTERS (lecture et reecriture des chapitres)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && ffmpeg -version > /dev/null \
 && ffprobe -version > /dev/null

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

VOLUME ["/config"]

# Interface web de revue (WEB_PORT)
EXPOSE 8681

ENTRYPOINT ["python", "/app/main.py"]
