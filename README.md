# 📹 Twitch Recorder

Applicazione web self-hosted per registrare automaticamente stream Twitch quando vanno online.

Dashboard real-time con SSE, notifiche Telegram/Discord, qualità per canale con fallback automatico, smart retry, split automatico, import/export canali e API REST completa.

## Funzionalità

- Layout adattivo: su schermi larghi Canali e Registrazioni sono affiancati, con scroll indipendente; su schermi più piccoli restano impilati.
- Lista registrazioni stabile: le righe esistenti vengono aggiornate senza ricreare l'intera lista e l'ordine usa l'ora di inizio del file quando è presente nel nome, così i file REC non saltano mentre crescono.
- **Lista canali stabile** — le schede vengono aggiornate in modo incrementale durante gli eventi SSE, conservando pulsanti, selettori qualità e focus.
- **Ricerca e ordinamento** — filtra per nome o piattaforma e ordina per priorità, live prima, nome, ultima live o ultimo controllo.
- **Filtri rapidi** — menu per visualizzare solo online, REC attive, canali non in pausa, in pausa, addormentati, privati, rerun escluse, offline confermati oppure canali da verificare.
- **Esclusione rerun Twitch** — con `SKIP_RERUNS=true`, verifica il tag Twitch ufficiale e il prefisso esplicito del titolo tramite Helix; interrompe/salta la registrazione quando la rerun è confermata. Richiede `TWITCH_CLIENT_ID` e `TWITCH_CLIENT_SECRET`.
- **Annunci Twitch** — Streamlink recente filtra automaticamente gli annunci incorporati; `SKIP_TWITCH_ADS` mantiene l'opzione esplicita solo per versioni legacy che la supportano. Non può rimuovere con certezza gli overlay pubblicitari della pagina web.
- **Login opzionale** — una password impostata con `APP_PASSWORD` protegge dashboard, API, stream live e file registrati; se la variabile è vuota il login è disattivato. Le sessioni hanno durata configurabile, token CSRF e limite ai tentativi errati; la chiave viene conservata nel volume dati se `SECRET_KEY` non è configurata.
- **Cambio qualità sicuro** — se REC è attiva, il file corrente viene finalizzato e il registratore apre una nuova sessione con la qualità scelta; ogni nome file è riservato per evitare sovrascritture.
- **Elaborazione file opzionale** — conversione TS→MP4 tramite FFmpeg `-c copy` e deduplicazione dei segmenti conclusi; entrambe disattivate per default, e il TS originale viene conservato anche quando la conversione MP4 è attiva, salvo scelta esplicita.

- **Registrazione automatica** — monitora i canali con worker concorrenti limitati e avvia la registrazione quando vanno online; un singolo risultato negativo non basta a interromperla
- **Timestamp registrazione normalizzati** — FFmpeg rimuxa il flusso in copia prima di scriverlo, quindi il tempo del file parte dall’inizio della registrazione e la preview REC condivide lo stesso TS normalizzato
- **Monitor adattivo** — intervalli distinti per canali online, offline e controlli in errore; le verifiche manuali hanno priorità e lo stato intermedio di offline viene mostrato nella dashboard
- **Last live e timestamp leggibili** — `last_live` mostra l’ultima rilevazione online in forma relativa, con data e ora complete al passaggio del mouse; i timestamp in `channels.json` e nell’export JSON/CSV sono ISO-8601 UTC, mantenendo `last_online` per compatibilità
- **Diagnostica live** — mostra stato del monitor, richieste in corso, ultimo controllo, tentativi di riconnessione, errori, stato REC e modalità della preview
- **Una volta** — pulsante con icona a orologio per avviare la registrazione temporanea; dopo la fine della live attende fino a 2 ore un eventuale ritorno online, azzera l'attesa se il canale riparte e mette automaticamente in pausa il canale se non torna live, disattivando il pulsante
- **Dashboard live** — aggiornamenti in tempo reale via SSE (Server-Sent Events) senza refresh, con fallback a polling
- **Qualità per canale** — qualità configurabile per canale (best, 1080p60, 720p, ecc.) con fallback automatico alla migliore disponibile
- **Notifiche** — Telegram (con screenshot live dal CDN Twitch), Discord webhook e webhook HTTP generico per eventi online, offline, inizio e fine registrazione
- **Smart retry** — quando lo stream cade, tenta la riconnessione con tentativi e attesa configurabili prima di arrendersi
- **Split automatico** — divide i file al raggiungimento della dimensione massima e riavvia la registrazione senza interruzioni
- **Import/Export** — importa ed esporta la lista canali in JSON o CSV, direttamente dalla dashboard
- **Stato e piattaforma a colpo d’occhio** — pallino verde/rosso per lo stato online/offline senza badge testuali duplicati; stati distinti per verifica/errore/sonno e sigla della piattaforma con colore dedicato (TW, KI, YT)
- **Force check** — forza un controllo immediato per un singolo canale o tutti i canali
- **Watchdog** — thread dedicato che monitora il monitor e lo riavvia se muore o si blocca
- **Guarda live intelligente** — durante REC riusa lo stesso flusso tramite buffer circolare in memoria; se REC è ferma apre un flusso temporaneo, senza creare file, chiuso quando il player termina
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

Per abilitare l'accesso con password, imposta `APP_PASSWORD` nel file `.env`. È consigliato configurare anche una `SECRET_KEY` lunga e casuale, ad esempio generata con `python -c "import secrets; print(secrets.token_hex(32))"`; se è vuota, l'app salva una chiave persistente nel volume dati. Se `APP_PASSWORD` è vuota o assente, l'app resta accessibile senza login. `APP_SESSION_HOURS` regola la durata della sessione (default 12). Imposta `SESSION_COOKIE_SECURE=true` solo quando l'app è raggiunta tramite HTTPS; con HTTP in locale lascialo su `false`.

Per saltare le rerun di Twitch, imposta `SKIP_RERUNS=true` e configura `TWITCH_CLIENT_ID` e `TWITCH_CLIENT_SECRET` di un'app registrata nel [portale sviluppatori Twitch](https://dev.twitch.tv/console/apps). Il monitor usa l'API Helix e considera rerun il tag ufficiale `Rerun` o un titolo che inizi esplicitamente con `Rerun`/`[Rerun]`. Se l'API non è raggiungibile, lo stato viene trattato come incerto: non si avvia una nuova registrazione finché non è possibile verificarlo. Il riconoscimento è specifico per Twitch, perché le altre piattaforme non espongono uno stato rerun uniforme tramite l'integrazione attuale.

`SKIP_TWITCH_ADS=true` conserva il comportamento di filtro esplicito sulle vecchie versioni Streamlink che supportano `--twitch-disable-ads`. Con Streamlink 7.5 o successivo gli annunci incorporati vengono già filtrati automaticamente; questa opzione non garantisce la rimozione di eventuali overlay pubblicitari disegnati nella pagina web.

## Ambiente Preview (tag Docker `preview`)

Il branch `preview` usa un workflow GitHub Actions separato (`.github/workflows/docker-preview.yml`) che pubblica su GHCR:

- `ghcr.io/alessandromasone/twitch-recorder:preview`
- `ghcr.io/alessandromasone/twitch-recorder:preview-<commit>` per identificare una build precisa

Questo workflow **non pubblica né aggiorna il tag `latest`**. Il workflow di produzione continua a essere gestito separatamente dal branch `main` e dai tag di release.

Per avviare la preview sullo stesso host della versione stabile, dopo che la GitHub Action ha pubblicato l'immagine:

```bash
docker compose -f docker-compose.preview.yml pull
docker compose -f docker-compose.preview.yml up -d
```

La preview è raggiungibile su `http://localhost:5001` per default. Puoi cambiare la porta host impostando `PREVIEW_PORT` (ad esempio in `.env`). Usa `docker compose -f docker-compose.preview.yml logs -f` per i log e `docker compose -f docker-compose.preview.yml down` per fermarla.

La preview ha un container e un volume separati (`twitch-recorder-preview-data`), quindi non condivide i canali o le registrazioni con l'istanza `latest`. Prima di usarla, verifica che il pacchetto GHCR sia accessibile dal server; per un pacchetto privato, esegui `docker login ghcr.io`.

Per pubblicare una nuova preview, invia le modifiche al branch `preview`:

```bash
# Primo utilizzo, se il branch non esiste ancora in locale
git switch -c preview
git push -u origin preview

# Per gli aggiornamenti successivi
git switch preview
git push origin preview
```

La pubblicazione viene eseguita automaticamente; i pull request diretti al branch `preview` vengono compilati per verificarne la build ma non pubblicano immagini.

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
| `SKIP_RERUNS` | `false` | Esclude le rerun Twitch riconosciute; richiede `TWITCH_CLIENT_ID` e `TWITCH_CLIENT_SECRET` |
| `TWITCH_CLIENT_ID` | _(vuota)_ | Client ID di un'app Twitch, necessario per leggere i tag live via Helix quando `SKIP_RERUNS=true` |
| `TWITCH_CLIENT_SECRET` | _(vuota)_ | Secret della stessa app Twitch; mantenerlo segreto e non inserirlo nel repository |
| `SKIP_TWITCH_ADS` | `true` | Su Streamlink legacy abilita il filtro esplicito degli annunci Twitch; Streamlink ≥ 7.5 filtra gli annunci incorporati automaticamente |
| `CHECK_INTERVAL` | `60` | Intervallo generale di riferimento tra i controlli |
| `MONITOR_WORKERS` | `2` | Numero massimo di controlli simultanei (1–8) |
| `ONLINE_CHECK_INTERVAL` | `30` | Secondi tra controlli per canali online |
| `OFFLINE_CHECK_INTERVAL` | `60` | Secondi tra controlli per canali offline confermati |
| `ERROR_CHECK_INTERVAL` | `120` | Backoff per controlli falliti o incerti |
| `OFFLINE_CONFIRMATIONS` | `3` | Risposte offline certe da raccogliere prima di confermare la fine live (2–5) |
| `OFFLINE_CONFIRM_INTERVAL` | `5` | Secondi tra le verifiche di conferma offline |
| `RECORDER_OFFLINE_GRACE_SECONDS` | `15` | Attesa aggiuntiva dopo offline confermato prima di fermare REC |
| `LIVE_STREAM_BUFFER_BYTES` | `4194304` | Dimensione del buffer RAM che il player condivide con REC (4 MiB) |
| `MAX_FILE_SIZE` | `1932735283` | Dimensione massima file prima dello split (~1.8 GB) |
| `PORT` | `5000` | Porta del server web |
| `LOG_LEVEL` | `INFO` | Livello di log (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `APP_PASSWORD` | _(vuota)_ | Password di accesso al pannello; vuota per non richiedere il login |
| `SECRET_KEY` | generata all'avvio | Chiave di firma delle sessioni; impostala in `.env` per mantenere valide le sessioni tra riavvii |
| `SESSION_COOKIE_SECURE` | `false` | Marca il cookie di sessione come Secure; abilitalo solo dietro HTTPS |
| `APP_SESSION_HOURS` | `12` | Durata sessione autenticata in ore (1–720) |
| `AUTO_CONVERT` | `false` | Remuxa i file `.ts` completati in `.mp4` senza ricodifica |
| `DELETE_AFTER_CONVERT` | `false` | Elimina l'originale `.ts` solo se la conversione MP4 è riuscita |
| `DEDUP_ENABLED` | `false` | Deduplica registrazioni concluse usando fingerprint di dimensione e campioni del file |
| `DEDUP_BYTES` | `2097152` | Dimensione del campione usato dalla deduplicazione (almeno 64 KiB) |
| `DEDUP_FILE` | `/data/dedup_hashes.json` nel container | Percorso indice deduplicazione; se vuoto viene collocato accanto a `CHANNELS_FILE` |

### Resilienza

| Variabile | Default | Descrizione |
|---|---|---|
| `RECONNECT_WAIT` | `30` | Secondi di attesa tra un tentativo di riconnessione e l'altro |
| `RECONNECT_TRIES` | `3` | Numero massimo di tentativi prima di dichiarare lo stream terminato |
| `JITTER_MAX` | `10` | Jitter casuale in secondi aggiunto tra i cicli di monitoraggio |
| `CHECK_PAUSED_ROOMS` | legacy | Non più necessario: ogni canale non dormiente viene controllato anche quando la registrazione è in pausa. La modalità luna sospende i controlli di quel canale. |

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

| `/api/delete_recording` | POST | Elimina una registrazione (`{"filename":"..."}`) |
| `/live/<platform>/<room>` | GET | Flusso live diretto temporaneo per il player; non crea file e viene chiuso quando il player termina |
| `/health` | GET | Healthcheck con stato monitor, controlli in corso e tempi dell'ultimo risultato |

### Azioni su `/api/room`

La pausa normale ferma la registrazione ma mantiene attivo il monitor. L’azione `toggle_sleep` con l’icona a luna ferma la registrazione e sospende i controlli; al risveglio il monitor riparte senza avviare REC. Lo stato `sleeping` è persistito in `channels.json` ed è incluso nell’esportazione JSON/CSV.

Tutte le azioni accettano un JSON con `action` e opzionalmente `channel` e `quality`.

| Azione | Descrizione |
|---|---|
| `add` | Aggiunge un canale (`channel` obbligatorio, `quality` opzionale) |
| `remove` | Rimuove un canale e ferma la registrazione |
| `pause` | Mette in pausa un canale |
| `resume` | Riprende un canale in pausa e disattiva la modalità Una volta |
| `toggle_single_live` | Attiva/disattiva la modalità Una volta; la pausa automatica scatta dopo 2 ore offline confermate |
| `toggle_sleep` | Alterna A dormire: sospende i controlli; al risveglio riattiva il monitor senza avviare REC |
| `set_quality` | Cambia la qualità; se REC è attiva finalizza il file corrente e avvia una nuova sessione con un file distinto |
| `pause_all` | Mette in pausa tutti i canali |
| `resume_all` | Riprende tutti i canali |

## Architettura

- **Monitor** — scheduler con massimo `MONITOR_WORKERS` richieste in volo; pianifica ogni canale in modo indipendente, assegna priorità ai controlli manuali, e usa cadenze separate per live, offline ed errori
- **Conferma offline** — richiede `OFFLINE_CONFIRMATIONS` esiti offline certi; gli errori non vengono conteggiati e una finestra aggiuntiva protegge la registrazione da interruzioni brevi
- **Recorder** — un thread per canale che legge un unico flusso Streamlink e lo scrive contemporaneamente sul file e su un buffer circolare RAM per il player; gestisce split e riconnessione
- **Preview** — durante REC ascolta il buffer condiviso senza aprire una seconda sorgente; senza REC crea un flusso temporaneo indipendente e chiude i processi alla fine del player
- **Watchdog** — controlla il thread monitor e segnala richieste bloccate; `/health` restituisce lo stato del monitor e delle verifiche in corso
- **SSE** — broadcast asincrono dello stato a tutti i client connessi dopo ogni check e ogni azione

## Rilascio nuova versione

```bash
git tag v1.0.0
git push origin v1.0.0
```

La GitHub Action builda e pusha automaticamente l'immagine Docker multi-arch su GHCR.

## Licenza

MIT
