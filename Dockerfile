FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ffmpeg turns songs into Opus for /play's voice playback. LibreOffice lays
# out Word documents as pages for the AI to look at while editing them; the
# Carlito, Caladea and Liberation fonts take up the same space as Calibri,
# Cambria, Arial and Times New Roman, so lines wrap where Word wraps them.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libreoffice-writer-nogui \
        fonts-crosextra-carlito fonts-crosextra-caladea fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
# yt-dlp needs a JavaScript runtime to read YouTube.
COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "app.py"]
