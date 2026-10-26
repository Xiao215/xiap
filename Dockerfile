FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ffmpeg turns songs into Opus for /play's voice playback.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "app.py"]
