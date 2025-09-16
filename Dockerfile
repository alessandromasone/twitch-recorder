# Usa immagine slim di Python
FROM python:3.11-slim

# Imposta la directory di lavoro
WORKDIR /app

# Installa dipendenze di sistema (solo quelle necessarie)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copia requirements e installa dipendenze Python + streamlink
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt streamlink

# Copia il resto del progetto
COPY . .

# Variabili d'ambiente
ENV CHANNELS_FILE=/app/channels.json \
    RECORDINGS_DIR=/app/recordings \
    STREAM_QUALITY=best \
    CHECK_INTERVAL=60 \
    PORT=5000 \
    MAX_FILE_SIZE=1932735283

# Crea cartella recordings
RUN mkdir -p /app/recordings

# Espone la porta
EXPOSE 5000

# Avvia l'app
CMD ["python", "app.py"]
