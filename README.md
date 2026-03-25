# 📹 Twitch Recorder

Applicazione web per registrare automaticamente stream Twitch quando vanno online.

Dashboard con statistiche in tempo reale, split automatico dei file, interfaccia responsive con tema dark/light.

## Funzionalità

- **Registrazione automatica** — monitora i canali e avvia la registrazione quando sono online
- **Dashboard live** — statistiche in tempo reale (canali, registrazioni attive, spazio disco) con auto-refresh ogni 5 secondi
- **Split automatico** — divide i file quando raggiungono la dimensione massima configurata
- **Multi-canale** — registra più canali contemporaneamente
- **Gestione file** — scarica o elimina le registrazioni dall'interfaccia web
- **Docker ready** — immagine multi-arch (amd64 + arm64) con CI/CD automatizzato su GitHub
- **API REST** — endpoint `/api/status` per integrazioni esterne

## Avvio rapido con Docker

```bash
docker run -d \
  --name twitch-recorder \
  -p 5000:5000 \
  -v twitch-data:/data \
  ghcr.io/alessandromasone/twitch-recorder:latest
```

Apri [http://localhost:5000](http://localhost:5000) nel browser.

### Docker Compose

```bash
# Clona il repo
git clone https://github.com/alessandromasone/twitch-recorder.git
cd twitch-recorder

# Avvia
docker compose up -d
```

## Installazione locale

**Requisiti:** Python 3.10+, ffmpeg

```bash
git clone https://github.com/alessandromasone/twitch-recorder.git
cd twitch-recorder

python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env      # modifica a piacere
python app.py
```

## Configurazione

Tutte le opzioni sono configurabili via variabili d'ambiente o file `.env`:

| Variabile | Default | Descrizione |
|---|---|---|
| `STREAM_QUALITY` | `best` | Qualità stream (`best`, `worst`, `720p`, `480p`…) |
| `CHECK_INTERVAL` | `60` | Secondi tra un controllo e l'altro |
| `MAX_FILE_SIZE` | `1932735283` | Dimensione massima file prima dello split (~1.8 GB) |
| `PORT` | `5000` | Porta del server web |
| `RECORDINGS_DIR` | `recordings` | Cartella registrazioni |
| `FILE_EXTENSION` | `.ts` | Estensione file video |
| `LOG_LEVEL` | `INFO` | Livello di log (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

Esempio con Docker:

```bash
docker run -d \
  -p 8080:5000 \
  -e STREAM_QUALITY=720p \
  -e CHECK_INTERVAL=30 \
  -v twitch-data:/data \
  ghcr.io/alessandromasone/twitch-recorder:latest
```

## API

| Endpoint | Metodo | Descrizione |
|---|---|---|
| `/api/status` | GET | Stato completo: canali, registrazioni, disco, statistiche |
| `/health` | GET | Healthcheck (`{"status": "ok"}`) |

## Rilascio nuova versione

Il CI/CD è automatizzato con GitHub Actions. Per rilasciare una nuova versione:

```bash
git tag v1.0.0
git push origin v1.0.0
```

L'immagine Docker verrà buildata e pubblicata su GHCR con i tag `latest`, `1.0.0`, `1.0`, e `1`.

## Stack

- **Backend:** Flask + Gunicorn
- **Frontend:** Bootstrap 5, vanilla JS con auto-refresh
- **Recording:** Streamlink + FFmpeg
- **Container:** Docker multi-arch con HEALTHCHECK
- **CI/CD:** GitHub Actions → GitHub Container Registry

## Licenza

MIT
