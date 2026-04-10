# 📹 Twitch Recorder

Applicazione web per registrare automaticamente stream Twitch quando vanno online.

Dashboard in tempo reale, qualità per canale con fallback automatico, anteprima video nel browser, split automatico dei file.

## Funzionalità

- **Registrazione automatica** — monitora i canali e avvia la registrazione quando sono online
- **Qualità per canale** — imposta una qualità diversa per ogni canale (best, 1080p60, 720p, ecc.); se non disponibile, scala automaticamente alla migliore inferiore
- **Anteprima video** — guarda le registrazioni direttamente nel browser senza scaricare, con supporto seeking
- **Dashboard live** — aggiornamenti in tempo reale via SSE (Server-Sent Events)
- **Split automatico** — divide i file quando raggiungono la dimensione massima configurata
- **Smart retry** — riconnessione intelligente con tentativi configurabili
- **Multi-canale** — registra più canali contemporaneamente
- **Notifiche** — Telegram (con screenshot live), Discord e webhook per eventi online/offline/registrazione
- **Import/Export** — importa ed esporta canali in JSON o CSV
- **Watchdog** — riavvia automaticamente il monitor se si blocca o muore
- **Docker ready** — immagine multi-arch (amd64 + arm64) con CI/CD automatizzato
- **API REST** — endpoint completi per integrazioni esterne

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
| `JITTER_MAX` | `10` | Jitter massimo in secondi tra i cicli |
| `CHECK_PAUSED_ROOMS` | `false` | Controlla anche i canali in pausa |
| `RECONNECT_WAIT` | `30` | Secondi prima di un tentativo di riconnessione |
| `RECONNECT_TRIES` | `3` | Numero massimo di tentativi di riconnessione |

### Notifiche

| Variabile | Default | Descrizione |
|---|---|---|
| `NOTIFY_TYPE` | _(vuoto)_ | `discord`, `telegram`, `webhook` o vuoto per disattivare |
| `NOTIFY_EVENTS` | `online,offline,rec_start,rec_end` | Eventi da notificare |
| `NOTIFY_URL` | | URL webhook Discord o generico |
| `TELEGRAM_BOT_TOKEN` | | Token del bot Telegram |
| `TELEGRAM_CHAT_ID` | | Chat ID Telegram |
| `TELEGRAM_NOTIFY_PHOTO` | `true` | Invia screenshot della live nella notifica online (solo Telegram) |

## API

| Endpoint | Metodo | Descrizione |
|---|---|---|
| `/api/status` | GET | Stato completo: canali, registrazioni, disco, statistiche |
| `/api/stream` | GET | SSE — aggiornamenti live in tempo reale |
| `/api/room` | POST | Azioni sui canali (add/remove/pause/resume/set_quality/pause_all/resume_all) |
| `/api/check` | POST | Forza un check immediato per uno o tutti i canali |
| `/api/export` | GET | Esporta canali in JSON o CSV (`?format=csv`) |
| `/api/import` | POST | Importa canali da JSON o CSV |
| `/api/toggle_check_paused` | POST | Attiva/disattiva il controllo dei canali in pausa |
| `/api/delete_recording` | POST | Elimina una registrazione |
| `/preview/<file>` | GET | Streaming video con supporto byte-range |
| `/health` | GET | Healthcheck con stato monitor e watchdog |

## Rilascio nuova versione

```bash
git tag v1.0.0
git push origin v1.0.0
```

## Licenza

MIT
