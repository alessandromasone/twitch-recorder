# 📹 Twitch Recorder

Applicazione web self-hosted che registra automaticamente le dirette di **Twitch**, **Kick** e **YouTube** appena vanno in onda, con una dashboard in tempo reale per gestire canali, registrazioni e anteprime.

- **Registrazione automatica.** Ogni canale può essere in REC, in pausa (controllato ma non registrato) o a dormire (né controllato né registrato). La modalità *Una volta* registra solo la prossima diretta. La fine della live viene confermata con più controlli, le riconnessioni sono automatiche e i file vengono divisi raggiunta una dimensione massima.
- **Leggera.** Con le credenziali Twitch i canali si controllano tutti insieme con l'API ufficiale: una richiesta ogni 100 canali. La dashboard riceve solo le modifiche, e senza browser aperti non calcola nulla.
- **Rerun riconosciute anche quando non sono dichiarate**, con un punteggio e una politica per canale.
- **Dashboard** con le dirette in copertina, comandi rapidi, ricerca, filtri, selezione multipla e una versione minimal di solo testo.
- **Player integrato**, anche da telefono, che durante la REC usa il flusso già scaricato senza aprire un'altra connessione.
- **Notifiche** su Telegram, Discord o webhook. Login opzionale. Immagine Docker per amd64 e arm64.

## Installazione

Con Docker Compose:

```bash
git clone https://github.com/alessandromasone/twitch-recorder.git
cd twitch-recorder
cp .env.example .env      # imposta almeno APP_PASSWORD e le credenziali Twitch
docker compose up -d
```

Oppure con un solo comando:

```bash
docker run -d --name twitch-recorder -p 5000:5000 -v twitch-data:/data \
  ghcr.io/alessandromasone/twitch-recorder:latest
```

Il pannello risponde su [http://localhost:5000](http://localhost:5000). Canali, storico e registrazioni stanno nel volume `/data`.

Per restare su una versione precisa invece che su `latest`, imposta `TAG=2.0.0` nel `.env`: Compose usa `ghcr.io/…/twitch-recorder:${TAG}`.

### Credenziali Twitch (consigliate)

1. Crea un'app nel [portale sviluppatori Twitch](https://dev.twitch.tv/console/apps). Come URL di redirect va bene `http://localhost`.
2. Copia *Client ID* e *Client Secret* in `TWITCH_CLIENT_ID` e `TWITCH_CLIENT_SECRET`.

Con le credenziali ottieni:
- controlli in blocco molto più leggeri;
- titolo, categoria, spettatori, copertina e icona di ogni canale;
- il rilevamento rerun completo, che usa anche VOD e spettatori.

Senza credenziali tutto funziona lo stesso: ogni controllo usa un processo Streamlink.

### Accesso

Imposta `APP_PASSWORD` per proteggere il pannello. Se l'app è raggiungibile in HTTPS, metti anche `SESSION_COOKIE_SECURE=true`. La chiave delle sessioni viene generata e salvata nel volume; puoi fissarla con `SECRET_KEY`, per esempio generata con `python -c "import secrets; print(secrets.token_hex(32))"`.

## Uso

### Canali
Scrivi il nome o incolla il link del canale e scegli piattaforma e qualità. Se la qualità scelta non esiste, viene usata la più vicina: prima la stessa risoluzione con altri FPS, poi quella inferiore, poi la superiore.

Ogni scheda e riga ha i comandi sempre visibili:

| Comando | Cosa fa |
|---|---|
| Stato REC | Rosso pieno: sta registrando. Pallino rosso: in attesa della diretta. Pausa: controllato ma non registrato. Luna: dorme. Un clic cambia stato. |
| Orologio | *Una volta*: registra la prossima live (o quella in corso), poi mette in pausa se il canale non torna in onda entro 2 ore. |
| Luna | *A dormire*: niente controlli e niente registrazioni finché non lo risvegli. |
| Freccia circolare | Controlla subito il canale. |
| ⋯ | Dettagli: qualità, politica rerun, diagnostica, rimozione. |

Selezione multipla (tasto `s`, con «Seleziona tutti» sui canali visibili), ricerca per nome, titolo o gioco, filtri con contatori, import ed export in JSON o CSV dalle impostazioni.

### Registrazioni
Raggruppate per giorno, con miniatura, durata, titolo e categoria della live ed eventuale segnalazione rerun. Si possono guardare nel browser, scaricare o eliminare; un file in registrazione non si può eliminare.

Sotto `MIN_FREE_GB` di spazio libero (predefinito 2) non partono nuove registrazioni, e la barra in alto lo segnala; quelle già in corso continuano.

### Spegnimenti improvvisi e storico
`channels.json` viene scritto in un file temporaneo, forzato su disco e solo allora sostituito: dopo uno spegnimento improvviso resta il file precedente completo oppure quello nuovo, mai un file vuoto. Se all'avvio il file risulta comunque illeggibile, viene conservato come `channels.json.danneggiato-<data>` e la lista si recupera dall'ultimo salvataggio interrotto o dalla copia più recente dello storico.

Lo storico (`backups/` accanto a `channels.json`) tiene le ultime `CHANNELS_BACKUPS` copie (predefinito 10) di nome, piattaforma, qualità e stato di ogni canale. Una copia nuova nasce solo quando cambia qualcosa che conta, non a ogni controllo. Dalle impostazioni (*Storico della lista*) vedi le copie e puoi ripristinarne una: i canali che non contiene vengono tolti dalla lista, le registrazioni restano sul disco, e la lista attuale finisce a sua volta nello storico.

### Player
Durante la REC il player usa il flusso già scaricato; altrimenti apre un flusso temporaneo, condiviso fra tutte le finestre e chiuso quando l'ultima viene chiusa. Non crea mai file.

Ha i controlli propri:
- volume ricordato;
- barra di avanzamento per i file;
- **In diretta**, per tornare al punto live;
- picture-in-picture e schermo intero.

Se il flusso resta fermo per 15 secondi si riconnette da solo.

Sul telefono si apre a tutto schermo, come nelle app video:
- un tocco mostra o nasconde i controlli;
- trascinando verso il basso si chiude;
- sui file, un doppio tocco ai lati va indietro o avanti di 10 secondi.

### Aspetto
- **Minimal:** un'unica tabella compatta di solo testo, con le dirette in cima. Predefinita con `MINIMAL_UI=true`, oppure si sceglie per browser da Impostazioni → Aspetto.
- **Nascondi copertine:** il pulsante a forma di occhio in alto, o il tasto `h`, copre copertine, miniature e video, sostituisce i titoli con "Titolo nascosto" e mostra l'iniziale al posto dell'icona del canale. È solo estetico: audio e registrazioni continuano, e la scelta resta anche ricaricando la pagina.
- **Scorciatoie:** `/` cerca, `s` selezione multipla, `h` nascondi copertine, `Esc` chiude. Nel player: `spazio` play/pausa, `m` audio, `f` schermo intero, `←` `→` ±10 s.

## Rerun

Twitch segnala una rerun solo con il tag ufficiale, che molti non usano. Per questo ogni live riceve un punteggio da segnali leggeri, senza analisi audio o video:

| Segnale | Punti |
|---|---|
| Tag ufficiale *Rerun*, tipo stream o titolo che inizia con "Rerun" | rerun confermata |
| *replica*, *rerun*, *rewatch*, *differita*, *registrata* nel titolo | +45 (*vod*, *highlights*, *best of*: +20) |
| Titolo identico a una live o a una VOD di almeno 3 ore prima | +45 (quasi identico, ≥ 92%: +30) |
| Stessa categoria della live corrispondente | +10 |
| Spettatori sotto il 25% della mediana del canale, dopo 15 minuti (solo con Helix) | +20 (sotto il 40%: +10) |
| Orario d'inizio mai visto nello storico (almeno 8 live) | +15 |

Da 40 punti la live è una rerun **possibile**, da 70 **probabile**; le soglie si cambiano con `RERUN_SUSPECT_SCORE` e `RERUN_PROBABLE_SCORE`. I titoli fissi che il canale riusa spesso non contano, e nemmeno una live ripartita entro 3 ore dalla precedente, per esempio dopo un crash.

La politica si sceglie per canale (predefinita: `RERUN_DEFAULT_POLICY`):
- `mark`: registra e segna. Non si perde mai una live.
- `skip`: non registra le rerun confermate né quelle probabili.
- `record`: ignora i sospetti.

Con `SKIP_RERUNS=true` le rerun dichiarate vengono sempre saltate.

Dalla scheda o dal dettaglio puoi rispondere **È rerun** o **Non lo è**; con la seconda il titolo viene ignorato in futuro per quel canale. Lo storico usato per i confronti è in `channel_history.json`.

## Configurazione

Tutte le variabili sono opzionali; l'elenco completo con i commenti è in [`.env.example`](.env.example).

### Generali e accesso

| Variabile | Default | Descrizione |
|---|---|---|
| `TAG` | `latest` | Versione dell'immagine usata da `docker-compose.yml` |
| `CHANNELS_FILE` | `channels.json` | File dei canali; gli altri file di stato vengono salvati accanto |
| `RECORDINGS_DIR` | `recordings` | Cartella delle registrazioni |
| `FILE_EXTENSION` / `FILENAME_FORMAT` | `.ts` / `{name}_{timestamp}{ext}` | Nome dei file |
| `STREAM_QUALITY` | `best` | Qualità predefinita per i nuovi canali |
| `MAX_FILE_SIZE` | `1932735283` | Dimensione oltre cui il file viene diviso (~1,8 GB) |
| `CHANNELS_BACKUPS` | `10` | Copie della lista canali nello storico (`backups/`); `0` disattiva |
| `MIN_FREE_GB` | `2` | Sotto questo spazio libero non partono nuove REC; `0` disattiva |
| `APP_PASSWORD` | _(vuota)_ | Password del pannello; vuota = nessun login |
| `SECRET_KEY` | generata | Chiave delle sessioni |
| `SESSION_COOKIE_SECURE` | `false` | `true` se l'app è servita in HTTPS |
| `APP_SESSION_HOURS` | `12` | Durata della sessione (1–720 ore) |
| `PORT` / `LOG_LEVEL` | `5000` / `INFO` | Porta e livello di log |

### Twitch e rerun

| Variabile | Default | Descrizione |
|---|---|---|
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | _(vuote)_ | Credenziali dell'app Twitch |
| `TWITCH_CHECK_METHOD` | `auto` | `auto` (Helix se ci sono le credenziali), `helix` o `streamlink` |
| `HELIX_OFFLINE_CHECK_INTERVAL` | `20` | Secondi tra i controlli dei canali Twitch offline con Helix (minimo 10) |
| `SKIP_RERUNS` | `false` | Salta sempre le rerun dichiarate |
| `RERUN_DEFAULT_POLICY` | `mark` | `mark`, `skip` o `record` |
| `RERUN_SUSPECT_SCORE` / `RERUN_PROBABLE_SCORE` | `40` / `70` | Soglie del punteggio |
| `SKIP_TWITCH_ADS` | `true` | Passa `--twitch-disable-ads` solo alle versioni di Streamlink che lo supportano |

Helix segnala l'inizio di una live con qualche decina di secondi di ritardo. Abbassare `HELIX_OFFLINE_CHECK_INTERVAL` riduce l'attesa e costa sempre una sola richiesta. In alternativa `TWITCH_CHECK_METHOD=streamlink` controlla ogni canale con Streamlink, che non ha ritardo ma è più pesante.

### Monitor, preview e file

| Variabile | Default | Descrizione |
|---|---|---|
| `MONITOR_WORKERS` | `2` | Controlli Streamlink simultanei (1–8) |
| `ONLINE_CHECK_INTERVAL` / `OFFLINE_CHECK_INTERVAL` / `ERROR_CHECK_INTERVAL` | `30` / `60` / `120` | Secondi tra i controlli |
| `OFFLINE_CONFIRMATIONS` / `OFFLINE_CONFIRM_INTERVAL` | `3` / `5` | Conferme necessarie per chiudere una live |
| `RECORDER_OFFLINE_GRACE_SECONDS` | `15` | Attesa extra prima di fermare la REC |
| `RECONNECT_WAIT` / `RECONNECT_TRIES` | `30` / `3` | Riconnessione quando il flusso cade |
| `LIVE_STREAM_BUFFER_BYTES` | `8388608` | Buffer RAM condiviso fra REC e preview (abbastanza per una copertina anche a 1080p60) |
| `RECORDING_STALL_SECONDS` | `180` | Se non arrivano dati per questi secondi la REC viene chiusa e il canale ricontrollato (su Twitch con `SKIP_TWITCH_ADS` almeno 300, per le pause pubblicitarie); `0` disattiva |
| `PREVIEW_STARTUP_TIMEOUT_SECONDS` / `PREVIEW_IDLE_TIMEOUT_SECONDS` | `120` / `180` | Tolleranza della preview (per esempio durante le pubblicità) |
| `PREVIEW_LINGER_SECONDS` | `10` | La sorgente temporanea resta aperta dopo l'ultimo spettatore |
| `AUTO_CONVERT` / `DELETE_AFTER_CONVERT` | `false` / `false` | Remux TS→MP4 a fine file; elimina il TS solo se la conversione riesce |
| `DEDUP_ENABLED` | `false` | Elimina i file conclusi identici a uno già registrato |
| `RECORDING_THUMBS` | `true` | Miniature dei file conclusi nella lista registrazioni (`false` con `MINIMAL_UI`). Sono generate al volo e tenute solo in memoria: su disco non viene salvato nulla |
| `RECORDING_LOGS` | `true` | Log di Streamlink e FFmpeg per canale in `recordings/.<canale>.log`; con `false` non vengono scritti e quelli vecchi vengono eliminati all'avvio |
| `RECORDING_LOG_MAX_MB` | `5` | Oltre questa dimensione il log viene ruotato in `.log.1` (al massimo due file per canale) |
| `MINIMAL_UI` | `false` | Versione minimal predefinita |

I segmenti più piccoli di 4 KB, per esempio quando una live non parte, vengono eliminati. Con `RECORDING_LOGS=false` la cartella delle registrazioni contiene solo i video: i metadati (titolo, categoria, rerun) stanno in `recordings_meta.json` accanto a `channels.json`.

### Notifiche

| Variabile | Default | Descrizione |
|---|---|---|
| `NOTIFY_TYPE` | _(vuoto)_ | `telegram`, `discord`, `webhook` o vuoto |
| `NOTIFY_EVENTS` | `online,offline,rec_start,rec_end` | Eventi da notificare |
| `NOTIFY_URL` | | Webhook Discord o generico |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | | Bot e chat Telegram |
| `TELEGRAM_NOTIFY_PHOTO` | `true` | Screenshot della live nella notifica online |

## API

| Endpoint | Metodo | Descrizione |
|---|---|---|
| `/api/status` | GET | Stato completo: canali, registrazioni, disco, configurazione, statistiche |
| `/api/stream` | GET | SSE: evento `status` (completo), poi `delta` (solo le differenze) |
| `/api/room` | POST | Azioni sui canali (vedi sotto) |
| `/api/check` | POST | Controllo immediato: `{"channel":"nome"}` o `{}` per tutti |
| `/api/export` | GET | Canali in JSON, o CSV con `?format=csv` |
| `/api/import` | POST | Importa JSON o CSV; `update_existing_quality=true` aggiorna la qualità dei canali esistenti |
| `/api/delete_recording` | POST | `{"filename":"..."}` oppure `{"filenames":[...]}` |
| `/live/<platform>/<room>` | GET | Flusso MPEG-TS del player |
| `/thumb/live/<room>.jpg` | GET | Fotogramma recente dal flusso in registrazione |
| `/thumb/rec/<file>.jpg` | GET | Miniatura di un file concluso |
| `/recordings/<file>` | GET | Streaming con Range; `?dl=1` per scaricare |
| `/health` | GET | Stato del monitor e versione |

Con il login attivo, le richieste POST richiedono l'intestazione `X-CSRF-Token`.

Azioni di `/api/room`. Il corpo JSON contiene `action`, `channel` e i parametri dell'azione:

| Azione | Parametri | Descrizione |
|---|---|---|
| `add` | `channel`, `platform`, `quality` | Aggiunge un canale (accetta anche il link) |
| `remove` | `channel` | Rimuove il canale; le registrazioni restano |
| `pause` / `resume` | `channel` | Pausa o ripresa della registrazione |
| `toggle_sleep` | `channel` | A dormire / risveglia |
| `toggle_single_live` | `channel` | Attiva o disattiva *Una volta* |
| `set_quality` | `channel`, `quality` | Durante la REC chiude il file e ne apre uno nuovo |
| `set_rerun_policy` | `channel`, `policy` | `mark`, `skip`, `record` o `null` |
| `rerun_override` | `channel`, `verdict` | `rerun`, `original` o `null` per la live corrente |
| `bulk` | `channels`, `op`, `value` | `op`: `pause`, `resume`, `sleep`, `wake`, `check`, `quality`, `rerun_policy`, `remove` |
| `pause_all` / `resume_all` | | Su tutti i canali |

## Sviluppo e rilasci

### Branch e immagini

| Evento | Cosa succede |
|---|---|
| Pull request o push su `main` | Solo i test. Nessuna immagine. |
| Push su `preview` | Test, poi l'immagine `:preview` viene sostituita. Le build superate da un push successivo si annullano. |
| Tag `vX.Y.Z` | Test, poi le immagini `X.Y.Z`, `X.Y` e `latest`, più una GitHub Release con le note prese da [`CHANGELOG.md`](CHANGELOG.md). |
| Ogni lunedì (o a mano) | Le versioni senza tag in GHCR vengono eliminate, tenendo le 10 più recenti. |

Le modifiche che toccano solo la documentazione non avviano build.

### Pubblicare una versione

1. Unisci le modifiche su `main`, per esempio con una pull request da `preview`.
2. In `CHANGELOG.md` sposta le voci da "Non rilasciato" a una nuova sezione `## X.Y.Z`.
3. Crea e invia il tag:
   ```bash
   git switch main && git pull
   git tag v2.0.0
   git push origin v2.0.0
   ```

La GitHub Action pubblica l'immagine e la Release. Per ripubblicare un tag esistente c'è il pulsante *Run workflow* nel workflow **Release**.

### Provare la preview

```bash
docker compose -f docker-compose.preview.yml pull
docker compose -f docker-compose.preview.yml up -d
```

La preview risponde su `http://localhost:5001` (porta cambiabile con `PREVIEW_PORT`), con un container e un volume separati dalla versione stabile.

### In locale

```bash
pip install -r requirements.txt pytest
python -m pytest -q        # test con uno Streamlink finto, senza rete
python app.py              # pannello su http://localhost:5000 (servono ffmpeg e streamlink)
```

```
app.py              punto d'ingresso (gunicorn app:app)
twrec/config.py     variabili d'ambiente
twrec/state.py      stato condiviso e salvataggio di channels.json
twrec/checks.py     controlli online (Helix in blocco, Streamlink)
twrec/twitch_api.py client Helix (token, stream, utenti, VOD)
twrec/monitor.py    pianificazione dei controlli, conferma offline, avvio/stop REC
twrec/recorder.py   Streamlink → FFmpeg → file + buffer condiviso
twrec/preview.py    player condiviso e fotogrammi live
twrec/rerun.py      punteggio rerun e storico
twrec/storage.py    registrazioni, metadati, miniature, spazio, dedup, MP4
twrec/status.py     stato JSON e SSE incrementale
twrec/actions.py    operazioni sui canali
twrec/web.py        Flask: login, pagine, API
static/             app.css, app.js (nessuna dipendenza)
tests/              pytest
```

## Licenza

MIT
