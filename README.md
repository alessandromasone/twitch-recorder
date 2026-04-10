# 📹 Twitch Recorder

Applicazione web self-hosted per registrare automaticamente stream Twitch quando vanno online.

Dashboard real-time con SSE, notifiche Telegram/Discord, qualità per canale con fallback automatico, smart retry, split automatico, import/export canali e API REST completa.

## Funzionalità

- **Registrazione automatica** — monitora i canali e avvia la registrazione quando vanno online, li ferma quando vanno offline
- **Dashboard live** — aggiornamenti in tempo reale via SSE (Server-Sent Events) senza refresh, con fallback a polling
- **Qualità per canale** — qualità configurabile per canale (best, 1080p60, 720p, ecc.) con fallback automatico alla migliore disponibile
- **Notifiche** — Telegram (con screenshot live dal CDN Twitch), Discord webhook e webhook HTTP generico per eventi online, offline, inizio e fine registrazione
- **Smart retry** — quando lo stream cade, tenta la riconnessione con tentativi e attesa configurabili prima di arrendersi
- **Split automatico** — divide i file al raggiungimento della dimensione massima e riavvia la registrazione senza interruzioni
- **Import/Export** — importa ed esporta la lista canali in JSON o CSV, direttamente dalla dashboard
- **Ordinamento canali** — ordina per stato (live first), nome o ordine di inserimento
- **Force check** — forza un controllo immediato per un singolo canale o tutti i canali
- **Watchdog** — thread dedicato che monitora il monitor e lo riavvia se muore o si blocca
- **Anteprima video** — streaming video nel browser con supporto byte-range per seeking
- **Multi-canale** — registra più canali contemporaneamente con thread pool
- **Protezione file** — impedisce l'eliminazione di registrazioni in corso
- **Docker ready** — immagine multi-arch (amd64 + arm64) con healthcheck e CI/CD automatizzato
- **API REST** — API JSON completa per integrazioni esterne e automazione

## Avvio rapido

### Docker

```bash
docker run -d \
  --name twitch-recorder \
  -p 5000:5000 \
  -v twitch-data:/data \
  ghcr.io/alessandromasone/twitch-recorder:latest
```

### Docker Compose

```bash
git clone https://github.com/alessandromasone/twitch-recorder.git
cd twitch-recorder
cp .env.example .env    # personalizza le variabili
docker compose up -d
```

Apri [http://localhost:5000](http://localhost:5000) nel browser.

## Configurazione

Tutte le variabili sono opzionali e hanno un valore di default. Si configurano tramite file `.env` o variabili d'ambiente.

### Generali

| Variabile | Default | Descrizione |
|---|---|---|
| `CHANNELS_FILE` | `channels.json` | File di salvataggio canali |
| `RECORDINGS_DIR` | `recordings` | Cartella registrazioni |
| `FILE_EXTENSION` | `.ts` | Estensione file video |
| `FILENAME_FORMAT` | `{name}_{timestamp}{ext}` | Pattern nome file |
| `STREAM_QUALITY` | `best` | Qualità default per nuovi canali |
| `CHECK_INTERVAL` | `60` | Secondi tra un ciclo di controllo e l'altro |
| `MAX_FILE_SIZE` | `1932735283` | Dimensione massima file prima dello split (~1.8 GB) |
| `PORT` | `5000` | Porta del server web |
| `LOG_LEVEL` | `INFO` | Livello di log (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

### Resilienza

| Variabile | Default | Descrizione |
|---|---|---|
| `RECONNECT_WAIT` | `30` | Secondi di attesa tra un tentativo di riconnessione e l'altro |
| `RECONNECT_TRIES` | `3` | Numero massimo di tentativi prima di dichiarare lo stream terminato |
| `JITTER_MAX` | `10` | Jitter casuale in secondi aggiunto tra i cicli di monitoraggio |
| `CHECK_PAUSED_ROOMS` | `false` | Se `true`, controlla lo stato anche dei canali in pausa |

### Notifiche

| Variabile | Default | Descrizione |
|---|---|---|
| `NOTIFY_TYPE` | _(vuoto)_ | Tipo di notifica: `discord`, `telegram`, `webhook` o vuoto per disattivare |
| `NOTIFY_EVENTS` | `online,offline,rec_start,rec_end` | Eventi da notificare (separati da virgola) |
| `NOTIFY_URL` | | URL del webhook Discord o generico |
| `TELEGRAM_BOT_TOKEN` | | Token del bot Telegram |
| `TELEGRAM_CHAT_ID` | | Chat ID Telegram |
| `TELEGRAM_NOTIFY_PHOTO` | `true` | Invia screenshot della live nella notifica online (solo Telegram) |

#### Formato notifiche Telegram

**Online** — foto con screenshot della live + nome canale cliccabile + 🟢 Online

**Offline** — messaggio testuale con nome canale cliccabile + 🔴 Offline

**rec_start / rec_end** — messaggio testuale con nome canale cliccabile e dettaglio

## API

Tutti gli endpoint restituiscono JSON.

| Endpoint | Metodo | Descrizione |
|---|---|---|
| `/api/status` | GET | Stato completo: canali, registrazioni, disco, config, statistiche |
| `/api/stream` | GET | SSE — stream di eventi per aggiornamenti live in tempo reale |
| `/api/room` | POST | Azioni sui canali (vedi sotto) |
| `/api/check` | POST | Forza un check immediato (`{"channel":"nome"}` o `{}` per tutti) |
| `/api/export` | GET | Esporta canali in JSON (default) o CSV (`?format=csv`) |
| `/api/import` | POST | Importa canali da file JSON o CSV (multipart o body) |
| `/api/toggle_check_paused` | POST | Toggle del controllo canali in pausa |
| `/api/delete_recording` | POST | Elimina una registrazione (`{"filename":"..."}`) |
| `/preview/<file>` | GET | Streaming video con supporto byte-range per seeking |
| `/health` | GET | Healthcheck con stato monitor e ultimo ciclo |

### Azioni su `/api/room`

Tutte le azioni accettano un JSON con `action` e opzionalmente `channel` e `quality`.

| Azione | Descrizione |
|---|---|
| `add` | Aggiunge un canale (`channel` obbligatorio, `quality` opzionale) |
| `remove` | Rimuove un canale e ferma la registrazione |
| `pause` | Mette in pausa un canale |
| `resume` | Riprende un canale in pausa |
| `set_quality` | Cambia la qualità di un canale |
| `pause_all` | Mette in pausa tutti i canali |
| `resume_all` | Riprende tutti i canali |

## Architettura

- **Monitor** — thread daemon che cicla ogni `CHECK_INTERVAL` secondi, controlla i canali online con un thread pool (8 worker) e avvia/ferma i recorder
- **Recorder** — un thread per canale che gestisce il processo `streamlink`, lo split per dimensione e la riconnessione intelligente
- **Watchdog** — thread daemon che ogni 60s verifica che il monitor sia vivo e non bloccato
- **SSE** — broadcast asincrono dello stato a tutti i client connessi dopo ogni check e ogni azione

## Rilascio nuova versione

```bash
git tag v1.0.0
git push origin v1.0.0
```

La GitHub Action builda e pusha automaticamente l'immagine Docker multi-arch su GHCR.

## Licenza

MIT
