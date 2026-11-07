# 📹 Twitch Recorder

Applicazione web self-hosted che registra automaticamente le dirette di Twitch, Kick e YouTube quando vanno online.

## Funzionalità

- **Registrazione automatica** con conferma della fine live su più controlli, riconnessione intelligente, split a dimensione massima e qualità per canale con fallback automatico (stessa risoluzione con FPS diversi, poi la più vicina inferiore, poi la superiore).
- **Controlli leggeri.** Con le credenziali Twitch, tutti i canali Twitch si controllano con l'API Helix: una sola richiesta HTTP ogni 100 canali, invece di un processo Streamlink per canale. Kick e YouTube usano Streamlink.
- **Rilevamento delle rerun, anche non dichiarate.** Ogni live riceve un punteggio da 0 a 100 basato su tag e titolo, titoli già trasmessi, VOD, spettatori e orario, con una politica per canale: registra e segna, salta, ignora. Vedi [Rerun](#rerun).
- **Multiview "In onda".** I canali in diretta compaiono come schede con la copertina di Twitch, etichetta REC/LIVE, titolo, categoria e spettatori. Gli altri canali sono righe compatte.
- **Preview condivisa.** Una sola sorgente per canale, qualunque sia il numero di finestre aperte. Durante la REC la preview usa il flusso già scaricato; senza REC apre un flusso temporaneo che si chiude quando l'ultima finestra viene chiusa. Non crea mai file.
- **Copertine senza costi.** Per Twitch l'immagine ufficiale restituita dall'API (o dal CDN pubblico); per Kick e YouTube in REC e per il file in corso nella lista registrazioni, un fotogramma dal flusso già in memoria; per i file conclusi una miniatura generata una sola volta.
- **Registrazioni con contesto:** raggruppate per giorno, con miniatura, durata, titolo e categoria della live e segnalazione rerun.
- **Comandi sempre a portata:** su ogni scheda e riga ci sono REC/pausa, *Una volta*, *A dormire*, controllo immediato e, per le rerun sospette, «È rerun» / «Non lo è».
- **Nascondi copertine:** il pulsante a forma di occhio in alto, o il tasto `h`, copre all'istante copertine, miniature, avatar e video del player (chiude anche il picture-in-picture). È solo estetico: audio e registrazioni continuano. La scelta resta anche ricaricando la pagina.
- **Versione minimal** con `MINIMAL_UI=true` o per singolo browser: un'unica lista densa con le dirette in cima, solo testo.
- **Player pulito:** controlli propri (play, volume ricordato, avanzamento per i file, "In diretta" per tornare al punto live, picture-in-picture, schermo intero, scorciatoie `spazio` `m` `f` `←` `→`) e riconnessione automatica se il flusso resta fermo per 15 secondi.
- **Gestione:** pannello di dettaglio per ogni canale, selezione multipla (pausa, registra, dormi, qualità, politica rerun, rimozione), ricerca anche per titolo e gioco, filtri con contatori, modalità *Una volta*, modalità *A dormire* e import/export in JSON e CSV.
- **Aggiornamenti in tempo reale** via SSE incrementale: dopo il primo stato completo arrivano solo i canali cambiati. Senza client collegati non viene calcolato nulla.
- **Login opzionale** con CSRF, limite ai tentativi e sessione persistente.
- **Notifiche** su Telegram (con screenshot e titolo), Discord e webhook generico.
- **Docker** multi-arch (amd64 + arm64) con healthcheck.

## Avvio rapido

```bash
docker run -d --name twitch-recorder -p 5000:5000 -v twitch-data:/data \
  ghcr.io/alessandromasone/twitch-recorder:latest
```

Oppure con Docker Compose:

```bash
git clone https://github.com/alessandromasone/twitch-recorder.git
cd twitch-recorder
cp .env.example .env    # personalizza le variabili
docker compose up -d
```

Apri [http://localhost:5000](http://localhost:5000).

**Consigliato:** crea un'app nel [portale sviluppatori Twitch](https://dev.twitch.tv/console/apps) (va bene un URL di redirect qualsiasi, per esempio `http://localhost`) e inserisci `TWITCH_CLIENT_ID` e `TWITCH_CLIENT_SECRET` nel `.env`. Così ottieni:
- controlli molto più leggeri;
- titolo, categoria, spettatori e avatar;
- il rilevamento rerun completo.

Per proteggere il pannello imposta `APP_PASSWORD` e una `SECRET_KEY` lunga, per esempio generata con `python -c "import secrets; print(secrets.token_hex(32))"`. Metti `SESSION_COOKIE_SECURE=true` solo dietro HTTPS.

## Rerun

Twitch segnala una rerun solo con il tag ufficiale, che molti non usano. Il recorder valuta quindi ogni live con segnali indipendenti, tutti leggeri (nessuna analisi audio o video):

| Segnale | Punti |
|---|---|
| Tag ufficiale *Rerun*, tipo stream o titolo che inizia con "Rerun" | rerun confermata |
| Parole come *replica*, *rerun*, *rewatch*, *differita*, *registrata* nel titolo | +45 (+20 per *vod*, *highlights*, *best of*) |
| Titolo identico a una live o a una VOD di almeno 3 ore prima | +45 (quasi identico, ≥ 92%: +30) |
| Stessa categoria della live corrispondente | +10 |
| Spettatori sotto il 25% della mediana del canale (dopo 15 minuti, solo con Helix) | +20 (sotto il 40%: +10) |
| Orario d'inizio mai visto nello storico del canale (almeno 8 live) | +15 |

Da 40 punti la live è una **possibile** rerun, da 70 una **probabile** (soglie modificabili con `RERUN_SUSPECT_SCORE` e `RERUN_PROBABLE_SCORE`).

Per evitare falsi positivi:
- i titoli "fissi", che il canale riusa in almeno 3 live o nel 30% delle live, non contano;
- una live ripartita dopo un crash, cioè entro 3 ore dalla precedente, non conta come copia.

Dal pannello del canale puoi indicare **È una rerun** o **Non è una rerun**. La seconda scelta mette quel titolo fra quelli da ignorare per il canale.

La politica si sceglie per canale, con il valore predefinito impostato da `RERUN_DEFAULT_POLICY`:

- `mark` (predefinito): registra comunque e segna il file e la scheda. Non perdi mai una live.
- `skip`: non registra le rerun confermate né quelle probabili. Se una REC è già in corso, viene fermata.
- `record`: ignora i sospetti.

Con `SKIP_RERUNS=true` le rerun **dichiarate** vengono sempre saltate, come nelle versioni precedenti.

Lo storico usato per i confronti è salvato in `channel_history.json` accanto a `channels.json`. Il rilevamento migliora man mano che il recorder osserva le live di un canale. Per un canale appena aggiunto, con Helix, i confronti usano anche le ultime 30 VOD.

## Leggerezza

- **Controlli:** con Helix, una richiesta HTTP ogni 100 canali al posto di un processo Python/Streamlink per canale a ogni controllo.
- **Avvio REC:** la qualità viene risolta direttamente da Streamlink con una lista di fallback (`720p60,720p,…,best`), senza una chiamata `--json` preliminare. La qualità effettivamente aperta si legge dal log.
- **SSE:**
  - viaggiano solo le differenze;
  - la lista registrazioni viene ripassata solo quando cambia, oppure ogni 30 secondi;
  - lo spazio su disco viene ricalcolato al massimo ogni 10 secondi;
  - gli aggiornamenti ravvicinati sono uniti, fino a circa 2 al secondo.
- **Scritture su disco:** `channels.json` viene scritto al massimo una volta ogni 2 secondi, invece che a ogni controllo.
- **Preview:** un solo processo per canale, condiviso fra gli spettatori.
- **Frontend:** niente Bootstrap, icone in sprite SVG, niente font web. `mpegts.js` è incluso nell'immagine Docker (scaricato durante la build, versione fissa) e viene caricato solo all'apertura del player. Gli asset statici hanno cache a lungo termine con versione nell'URL.

## Ambiente Preview (tag Docker `preview`)

Il branch `preview` usa un workflow separato (`.github/workflows/docker-preview.yml`) che pubblica:

- `ghcr.io/alessandromasone/twitch-recorder:preview`
- `ghcr.io/alessandromasone/twitch-recorder:preview-<commit>`

Il tag `latest` non viene mai toccato. Per avviare la preview accanto alla versione stabile:

```bash
docker compose -f docker-compose.preview.yml pull
docker compose -f docker-compose.preview.yml up -d
```

La preview risponde su `http://localhost:5001` (cambiabile con `PREVIEW_PORT`). Ha un container e un volume propri (`twitch-recorder-preview-data`), quindi non condivide canali o registrazioni con `latest`. Per un pacchetto GHCR privato esegui prima `docker login ghcr.io`.

Per pubblicare una nuova preview basta un push sul branch `preview`:

```bash
git switch preview
git push -u origin preview
```

## Configurazione

Tutte le variabili sono opzionali.

### Generali

| Variabile | Default | Descrizione |
|---|---|---|
| `CHANNELS_FILE` | `channels.json` | File dei canali (gli altri file di stato vengono salvati accanto) |
| `RECORDINGS_DIR` | `recordings` | Cartella registrazioni |
| `FILE_EXTENSION` | `.ts` | Estensione file video |
| `FILENAME_FORMAT` | `{name}_{timestamp}{ext}` | Pattern nome file |
| `STREAM_QUALITY` | `best` | Qualità predefinita per i nuovi canali |
| `MAX_FILE_SIZE` | `1932735283` | Dimensione massima prima dello split (~1,8 GB) |
| `MIN_FREE_GB` | `2` | Sotto questo spazio libero non partono nuove registrazioni; quelle in corso continuano. `0` disattiva |
| `PORT` | `5000` | Porta del server web |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `APP_PASSWORD` | _(vuota)_ | Password del pannello; vuota = nessun login |
| `SECRET_KEY` | generata e salvata | Chiave di firma delle sessioni |
| `SESSION_COOKIE_SECURE` | `false` | Cookie Secure (solo con HTTPS) |
| `APP_SESSION_HOURS` | `12` | Durata della sessione (1–720 ore) |

### Twitch e rerun

| Variabile | Default | Descrizione |
|---|---|---|
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | _(vuote)_ | Credenziali di un'app Twitch per l'API Helix |
| `TWITCH_CHECK_METHOD` | `auto` | `auto` (Helix se ci sono le credenziali), `helix` o `streamlink` |
| `HELIX_OFFLINE_CHECK_INTERVAL` | `20` | Secondi tra i controlli dei canali Twitch offline quando si usa Helix |
| `SKIP_RERUNS` | `false` | Salta sempre le rerun dichiarate |
| `RERUN_DEFAULT_POLICY` | `mark` | `mark`, `skip` o `record`; modificabile per canale |
| `RERUN_SUSPECT_SCORE` | `40` | Punteggio da cui una live è "possibile rerun" |
| `RERUN_PROBABLE_SCORE` | `70` | Punteggio da cui una live è "probabile rerun" |
| `SKIP_TWITCH_ADS` | `true` | Passa `--twitch-disable-ads` solo alle vecchie versioni di Streamlink che lo supportano |

Helix aggiorna lo stato delle live con qualche decina di secondi di ritardo rispetto a Streamlink. Se per te conta registrare dal primissimo secondo, imposta `TWITCH_CHECK_METHOD=streamlink`: perdi la leggerezza dei controlli in blocco, ma resti con i metadati via Streamlink e il rilevamento rerun basato sul titolo.

### Monitor e preview

| Variabile | Default | Descrizione |
|---|---|---|
| `MONITOR_WORKERS` | `2` | Controlli Streamlink simultanei (1–8) |
| `ONLINE_CHECK_INTERVAL` | `30` | Secondi tra i controlli dei canali online |
| `OFFLINE_CHECK_INTERVAL` | `60` | Secondi tra i controlli dei canali offline |
| `ERROR_CHECK_INTERVAL` | `120` | Attesa dopo un controllo fallito |
| `OFFLINE_CONFIRMATIONS` | `3` | Esiti offline necessari per confermare la fine della live (2–5) |
| `OFFLINE_CONFIRM_INTERVAL` | `5` | Secondi tra le verifiche di conferma |
| `RECORDER_OFFLINE_GRACE_SECONDS` | `15` | Attesa extra prima di fermare la REC |
| `RECONNECT_WAIT` / `RECONNECT_TRIES` | `30` / `3` | Riconnessione quando il flusso cade |
| `LIVE_STREAM_BUFFER_BYTES` | `4194304` | Buffer RAM condiviso fra REC e preview |
| `PREVIEW_STARTUP_TIMEOUT_SECONDS` | `120` | Attesa massima dei primi dati della preview |
| `PREVIEW_IDLE_TIMEOUT_SECONDS` | `180` | Pausa di output tollerata (per esempio durante le pubblicità) |
| `PREVIEW_LINGER_SECONDS` | `10` | La sorgente temporanea resta aperta per questi secondi dopo l'ultimo spettatore |

### File

| Variabile | Default | Descrizione |
|---|---|---|
| `AUTO_CONVERT` | `false` | Remux TS→MP4 senza ricodifica a fine file |
| `DELETE_AFTER_CONVERT` | `false` | Elimina il TS solo dopo una conversione riuscita |
| `DEDUP_ENABLED` | `false` | Elimina i file conclusi identici a uno già registrato |
| `DEDUP_BYTES` / `DEDUP_FILE` | `2097152` / accanto a `CHANNELS_FILE` | Campione e indice della deduplicazione |
| `RECORDING_THUMBS` | `true` (`false` con `MINIMAL_UI`) | Miniature dei file conclusi, in `recordings/.thumbs` |

### Interfaccia

| Variabile | Default | Descrizione |
|---|---|---|
| `MINIMAL_UI` | `false` | Versione minimal predefinita: un'unica lista densa di solo testo, senza copertine, avatar e animazioni. Ogni browser può cambiarla da Impostazioni → Aspetto |

I segmenti più piccoli di 4 KB, per esempio quando una live non parte, vengono eliminati automaticamente.

### Notifiche

| Variabile | Default | Descrizione |
|---|---|---|
| `NOTIFY_TYPE` | _(vuoto)_ | `discord`, `telegram`, `webhook` o vuoto |
| `NOTIFY_EVENTS` | `online,offline,rec_start,rec_end` | Eventi da notificare |
| `NOTIFY_URL` | | Webhook Discord o generico |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | | Bot e chat Telegram |
| `TELEGRAM_NOTIFY_PHOTO` | `true` | Screenshot della live nella notifica online (Twitch) |

## API

| Endpoint | Metodo | Descrizione |
|---|---|---|
| `/api/status` | GET | Stato completo: canali, registrazioni, disco, configurazione, statistiche |
| `/api/stream` | GET | SSE: evento `status` (completo) e poi `delta` (solo differenze) |
| `/api/room` | POST | Azioni sui canali (vedi sotto) |
| `/api/check` | POST | Controllo immediato (`{"channel":"nome"}` o `{}` per tutti) |
| `/api/export` | GET | Canali in JSON o CSV (`?format=csv`) |
| `/api/import` | POST | Importa JSON o CSV; `update_existing_quality=true` aggiorna la qualità dei canali esistenti |
| `/api/delete_recording` | POST | `{"filename":"..."}` oppure `{"filenames":[...]}` |
| `/live/<platform>/<room>` | GET | Flusso MPEG-TS della preview |
| `/thumb/live/<room>.jpg` | GET | Fotogramma recente dal flusso già acquisito |
| `/thumb/rec/<file>.jpg` | GET | Miniatura di una registrazione conclusa |
| `/recordings/<file>` | GET | Download o streaming (supporta Range; `?dl=1` per forzare il download) |
| `/health` | GET | Healthcheck del monitor |

Azioni di `/api/room`. Il corpo JSON contiene `action`, più `channel` e i parametri dell'azione.

| Azione | Parametri | Descrizione |
|---|---|---|
| `add` | `channel`, `platform`, `quality` | Aggiunge un canale (si può incollare il link) |
| `remove` | `channel` | Rimuove il canale; le registrazioni restano |
| `pause` / `resume` | `channel` | Pausa o ripresa della registrazione; il monitor continua |
| `toggle_sleep` | `channel` | A dormire: ferma la REC e sospende i controlli |
| `toggle_single_live` | `channel` | Una volta: registra la prossima live, poi pausa dopo 2 ore offline |
| `set_quality` | `channel`, `quality` | Durante la REC chiude il file e ne apre uno nuovo |
| `set_rerun_policy` | `channel`, `policy` | `mark`, `skip`, `record` o `null` (predefinita) |
| `rerun_override` | `channel`, `verdict` | `rerun`, `original` o `null` per la live corrente |
| `bulk` | `channels`, `op`, `value` | `op`: `pause`, `resume`, `sleep`, `wake`, `check`, `quality`, `rerun_policy`, `remove` |
| `pause_all` / `resume_all` | | Su tutti i canali |

## Struttura del codice

```
app.py              punto d'ingresso (gunicorn app:app)
twrec/config.py     variabili d'ambiente
twrec/state.py      stato condiviso e salvataggio di channels.json
twrec/checks.py     controlli online (Helix in blocco, Streamlink)
twrec/monitor.py    scheduler, conferma offline, avvio/stop REC
twrec/recorder.py   Streamlink → FFmpeg → file + buffer
twrec/preview.py    preview condivisa e miniature live
twrec/rerun.py      punteggio rerun e storico
twrec/storage.py    indice registrazioni, metadati, miniature, dedup, MP4
twrec/status.py     stato JSON e SSE incrementale
twrec/actions.py    operazioni sui canali
twrec/web.py        Flask: login, pagine, API
static/             app.css, app.js (nessuna dipendenza)
tests/              pytest con uno Streamlink finto
```

Per i test: `pip install pytest flask python-dotenv && python -m pytest -q`. Le GitHub Action eseguono gli stessi test prima di ogni build: se un test fallisce, l'immagine non viene pubblicata.

## Rilascio

```bash
git tag v1.0.0
git push origin v1.0.0
```

## Licenza

MIT
