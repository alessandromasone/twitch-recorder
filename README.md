# 📹 Twitch Recorder

Applicazione web per registrare automaticamente stream Twitch quando vanno online.

Dashboard in tempo reale, qualità per canale con fallback automatico, anteprima video nel browser, split automatico dei file.

## Funzionalità

- **Registrazione automatica** — monitora i canali e avvia la registrazione quando sono online
- **Qualità per canale** — imposta una qualità diversa per ogni canale (best, 1080p60, 720p, ecc.); se non disponibile, scala automaticamente alla migliore inferiore
- **Anteprima video** — guarda le registrazioni direttamente nel browser senza scaricare, con supporto seeking
- **Dashboard live** — statistiche in tempo reale con auto-refresh ogni 5 secondi
- **Split automatico** — divide i file quando raggiungono la dimensione massima configurata
- **Multi-canale** — registra più canali contemporaneamente
- **Docker ready** — immagine multi-arch (amd64 + arm64) con CI/CD automatizzato
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
git clone https://github.com/alessandromasone/twitch-recorder.git
cd twitch-recorder
docker compose up -d
```

## Configurazione

| Variabile | Default | Descrizione |
|---|---|---|
| `STREAM_QUALITY` | `best` | Qualità default per nuovi canali |
| `CHECK_INTERVAL` | `60` | Secondi tra un controllo e l'altro |
| `MAX_FILE_SIZE` | `1932735283` | Dimensione massima file (~1.8 GB) |
| `PORT` | `5000` | Porta del server web |
| `RECORDINGS_DIR` | `recordings` | Cartella registrazioni |
| `FILE_EXTENSION` | `.ts` | Estensione file video |
| `LOG_LEVEL` | `INFO` | Livello di log |

## API

| Endpoint | Metodo | Descrizione |
|---|---|---|
| `/api/status` | GET | Stato completo: canali, registrazioni, disco, statistiche |
| `/preview/<file>` | GET | Streaming video con supporto byte-range |
| `/health` | GET | Healthcheck |

## Rilascio nuova versione

```bash
git tag v1.0.0
git push origin v1.0.0
```

## Licenza

MIT
