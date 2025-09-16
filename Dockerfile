# Usa un'immagine leggera con Python 3.11
FROM python:3.11-slim

# Installa dipendenze di sistema necessarie per streamlink e altre utility
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Installa streamlink
RUN pip install --no-cache-dir streamlink

# Imposta la directory di lavoro
WORKDIR /app

# Copia requirements.txt e installa le dipendenze Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia il resto del progetto
COPY . .

# Espone la porta del server Flask
EXPOSE 5000

# Variabili d'ambiente default (possono essere sovrascritte in docker run)
ENV CHANNELS_FILE=/app/channels.json \
    RECORDINGS_DIR=/app/recordings \
    STREAM_QUALITY=best \
    CHECK_INTERVAL=60 \
    PORT=5000 \
    MAX_FILE_SIZE=1932735283

# Crea la cartella recordings
RUN mkdir -p /app/recordings

# Avvia l'app
CMD ["python", "app.py"]
