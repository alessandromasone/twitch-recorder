# Changelog

Le versioni seguono [Semantic Versioning](https://semver.org/lang/it/). Ogni sezione `## X.Y.Z`
diventa automaticamente la nota della GitHub Release quando si pubblica il tag `vX.Y.Z`.

## Non rilasciato

- **Kick: niente più "in REC" senza file quando il canale è offline.** Se streamlink si collega ma non arriva video, la registrazione non riparte più all'infinito: un segmento conta come riuscito solo se ha scritto almeno 256 KB, e dopo 3 minuti senza video la REC si chiude con un messaggio chiaro.
- **Pausa dopo un tentativo a vuoto**: se un'intera sessione non ha ricevuto video, il canale aspetta 5, 10, 20 e poi 30 minuti prima di riprovare. **Controlla ora** (o Registra) annulla l'attesa.
- **Stato più onesto nell'interfaccia**: il badge REC compare solo quando i dati arrivano davvero. Mentre il flusso si sta collegando, o se si ferma, il canale mostra "Connessione al flusso…" e il file vuoto non compare tra le registrazioni.

## 2.2.0

- **Salvataggio sicuro di `channels.json`**: ogni scrittura viene forzata su disco prima di sostituire il file, così uno spegnimento improvviso non lascia più un file vuoto o troncato.
- **Recupero automatico**: se all'avvio `channels.json` è illeggibile viene conservato come `channels.json.danneggiato-<data>` e la lista si recupera dal salvataggio interrotto o dalla copia più recente dello storico.
- **Storico della lista**: a ogni modifica di canali o stato (nome, piattaforma, qualità, REC, pausa, a dormire, Una volta, politica rerun) si salva una copia in `backups/`, tenendo le ultime 10 (`CHANNELS_BACKUPS`). I controlli periodici non creano copie. Dalle impostazioni si vedono le copie e se ne può ripristinare una.
- **Seleziona tutti** nella selezione multipla: seleziona (o deseleziona) tutti i canali visibili con il filtro e la ricerca correnti.
- **Registrazioni che restavano appese**:
  - lo stop chiude Streamlink insieme ai suoi processi figli (per esempio l'FFmpeg che usa per YouTube): prima potevano tenere aperto il flusso e la registrazione non si chiudeva più;
  - se non arrivano dati per 180 secondi (`RECORDING_STALL_SECONDS`; su Twitch con le pubblicità filtrate almeno 300) la registrazione viene chiusa e il canale ricontrollato;
  - un errore imprevisto nel thread di registrazione non lascia più il canale bloccato in REC; il watchdog libera comunque eventuali registrazioni rimaste senza thread;
  - allo stop del container le registrazioni vengono chiuse subito e in parallelo e si attende che i file siano chiusi (`stop_grace_period: 45s` nei docker-compose).
- **Kick e YouTube**:
  - copertine dal flusso in REC più affidabili ad alta risoluzione: il buffer condiviso passa a 8 MB (`LIVE_STREAM_BUFFER_BYTES`) e il fotogramma si cerca in tutto il buffer;
  - YouTube ha la copertina pubblica della diretta anche senza registrazione;
  - se la preview non parte il player mostra il motivo (rifiuto 403 della piattaforma, diretta appena finita, diretta privata) invece di un generico «connessione interrotta».

## 2.1.1

- Ordine dei controlli: prima quelli chiesti a mano e i canali in REC, poi quelli con la registrazione attiva, infine quelli in pausa (con Helix i canali Twitch restano in un'unica richiesta).
- Colonna Registrazioni: lista vuota con margini corretti, intestazione centrata e righe allineate (niente più spazio riservato alla barra di scorrimento).

## 2.1.0

- `RECORDING_LOGS=false` disattiva i file di log per canale (`recordings/.<canale>.log`) ed elimina all'avvio quelli rimasti; `RECORDING_LOG_MAX_MB` (predefinito 5) li ruota in `.log.1`, così non crescono più senza limiti.
- Le miniature dei file conclusi non vengono più salvate su disco: si generano al volo e restano solo in memoria (al massimo 300). La vecchia cartella `recordings/.thumbs` viene eliminata all'avvio.
- I metadati delle registrazioni passano da `recordings/.recordings_meta.json` a `recordings_meta.json` accanto a `channels.json` (spostati automaticamente).

## 2.0.0

Riscrittura completa: codice a moduli, controlli molto più leggeri, interfaccia nuova,
rilevamento delle rerun non dichiarate e player rifatto. `.env` e API restano compatibili.

### Leggerezza
- Con `TWITCH_CLIENT_ID` e `TWITCH_CLIENT_SECRET` i canali Twitch si controllano con l'API Helix: una richiesta ogni 100 canali invece di un processo Streamlink per canale. `TWITCH_CHECK_METHOD=streamlink` ripristina il metodo precedente.
- La qualità si risolve direttamente in Streamlink con una lista di fallback, senza sonda preliminare.
- Aggiornamenti SSE incrementali (solo i canali cambiati), indice delle registrazioni e spazio su disco in cache, `channels.json` scritto al massimo ogni 2 secondi; senza browser collegati non si calcola nulla.
- Preview condivisa: un solo processo per canale qualunque sia il numero di spettatori; durante la REC usa il flusso già scaricato.
- Frontend senza Bootstrap né font web; `mpegts.js` incluso nell'immagine Docker e caricato solo all'apertura del player.

### Interfaccia
- Schede "In onda" con la copertina di Twitch, stato REC/LIVE, titolo, categoria, spettatori e icona del canale; righe compatte per gli altri canali.
- Comandi rapidi sempre visibili (stato REC, Una volta, A dormire, Controlla ora) e pannello di dettaglio con qualità, politica rerun, diagnostica e rimozione.
- Selezione multipla, filtri con contatori, ricerca anche per titolo e gioco, ordinamento stabile che non sposta le schede a ogni controllo.
- Registrazioni raggruppate per giorno con miniatura, durata, titolo, categoria e segnalazione rerun; lista aggiornata senza ricreare le righe.
- Versione minimal (`MINIMAL_UI` o per browser): un'unica tabella compatta di solo testo.
- Pulsante occhio (tasto `h`): nasconde copertine, video, titoli e icone dei canali.
- Login ridisegnato, tema chiaro/scuro, layout a schede sul telefono.

### Player
- Controlli propri: volume ricordato, avanzamento per i file, ritorno alla diretta, picture-in-picture, schermo intero, scorciatoie da tastiera.
- Riconnessione automatica se il flusso resta fermo per 15 secondi; buffer allineato ai pacchetti MPEG-TS (causa dei blocchi precedenti).
- Sul telefono: foglio a tutto schermo come nelle app video, tocco per i controlli, trascinamento verso il basso per chiudere, doppio tocco per ±10 s, avvio muto con "Tocca per l'audio".

### Rerun
- Punteggio 0-100 da tag ufficiale, parole chiave, titoli e VOD già trasmessi, categoria, spettatori e orario, ignorando i titoli fissi del canale e le ripartenze dopo un crash.
- Politica per canale (registra e segna, salta, ignora), correzione manuale con "È rerun" / "Non lo è".

### Gestione e affidabilità
- `MIN_FREE_GB`: sotto la soglia di spazio libero non partono nuove registrazioni.
- Miniature dei file conclusi generate una sola volta; fotogrammi live dal flusso in REC con ripiego sulla copertina di Twitch.
- Versione dell'app visibile nelle impostazioni e in `/health`.

### Docker e rilasci
- Le immagini si pubblicano solo con un tag di versione (`X.Y.Z`, `X.Y`, `latest`); il branch `preview` aggiorna un'unica immagine `:preview`; i push su `main` eseguono solo i test.
- Test automatici prima di ogni build, pulizia settimanale delle immagini senza tag in GHCR.
- gunicorn con 32 thread (SSE e preview occupano un thread ciascuna); i file Compose passano anche le variabili delle notifiche, prima ignorate.

### Cambiamenti di comportamento
- I segmenti più piccoli di 4 KB (live non partite) vengono eliminati.
- `SKIP_RERUNS=true` senza credenziali Twitch non blocca più le registrazioni: riconosce le rerun dichiarate dal titolo.
- Nuove variabili: `TWITCH_CHECK_METHOD`, `HELIX_OFFLINE_CHECK_INTERVAL`, `RERUN_DEFAULT_POLICY`, `RERUN_SUSPECT_SCORE`, `RERUN_PROBABLE_SCORE`, `PREVIEW_LINGER_SECONDS`, `RECORDING_THUMBS`, `MINIMAL_UI`, `MIN_FREE_GB`.

## 1.x

Versioni precedenti alla riscrittura, dalla più recente.

### Versione aggiornata — selezione qualità coerente e import configurazione esistente
- Corretto il fallback qualità: cerca prima una variante alla stessa altezza video (FPS alternativi) prima di scendere di risoluzione; se non esistono varianti inferiori, considera quella superiore più vicina.
- Aggiunto `resolved_quality` nello stato runtime e indicatore in scheda quando Streamlink usa una qualità diversa da quella richiesta.
- Migliorati i log: riportano la qualità richiesta, la qualità selezionata e le varianti disponibili.
- Aggiunta all'importazione l'opzione esplicita per aggiornare la qualità dei canali già presenti, senza sovrascrivere pausa, sonno o modalità Una volta. Le REC attive vengono finalizzate e riavviate in file separato se la qualità cambia.
- Senza l'opzione, il comportamento precedente di saltare i duplicati resta invariato; importazioni di liste testuali prive del campo `quality` non modificano la qualità esistente.
- Test isolati sulla selezione tra 720p/720p60 e 1080p/1080p60; sintassi Python/JavaScript verificata. Test con dirette reali non eseguito in questo ambiente.

### Versione aggiornata — preview più resiliente durante gli annunci
- Aumentati i timeout della preview temporanea e condivisa: 120 secondi per ricevere i primi dati e 180 secondi di tolleranza per pause di output durante la live, configurabili tramite `.env`.
- Il player web ora tenta una riconnessione automatica con backoff limitato dopo errori di decodifica, fine inattesa del flusso o stalli prolungati; il contatore si azzera dopo una riproduzione stabile.
- Attivata la correzione dei gap dei timestamp audio in mpegts.js, spesso utili quando Streamlink riprende dopo aver filtrato segmenti pubblicitari.
- La preview temporanea mantiene lo stesso comportamento senza REC e non crea file; la chiusura manuale termina i processi temporanei.
- Verificata la sintassi Python/JavaScript e i file YAML; test di live reale non disponibile in questo ambiente.

### Versione aggiornata — esclusione rerun e configurazione annunci
- Aggiunto `SKIP_RERUNS` (disattivato per default) per escludere le rerun Twitch riconosciute tramite tag Helix ufficiale o prefisso del titolo.
- L'esclusione rerun richiede `TWITCH_CLIENT_ID` e `TWITCH_CLIENT_SECRET`; in caso di verifica incerta il monitor non avvia nuove registrazioni.
- Le rerun riconosciute hanno stato distinto nella UI, filtro dedicato e non aggiornano `last_live`; una registrazione eventualmente attiva viene fermata.
- Aggiunto `SKIP_TWITCH_ADS`; Streamlink recente filtra automaticamente gli annunci incorporati, mentre il flag legacy viene passato solo se la versione installata lo supporta.
- Rimosso l'uso incondizionato di `--twitch-disable-ads`, non più documentato nelle versioni recenti di Streamlink.
- Aggiornati `.env.example`, Docker Compose Preview/produzione e README.
- Test statici e unitari isolati eseguiti; il test end-to-end richiede l'ambiente completo con Flask, Streamlink, FFmpeg e credenziali Twitch API.

### Versione aggiornata — revisione UI, sessione e gestione file
- Layout desktop allineato a una griglia più proporzionata: canali più ampi delle registrazioni su desktop e liste con scroll indipendente dimensionato in base alla finestra.
- Scrollbar stabile e anchoring disattivato per evitare salti visivi nelle liste durante gli aggiornamenti.
- Scadenza sessione configurabile con APP_SESSION_HOURS; se SECRET_KEY non è fornita, una chiave casuale viene conservata nel volume dati per non invalidare le sessioni a ogni riavvio.
- Export/import conserva lo stato completo di "Una volta", inclusa la scadenza del timer; corretta una sovrascrittura che riportava i campi importati ai valori predefiniti.
- Aggiunti AUTO_CONVERT (TS→MP4 in copia, disattivato per default), DELETE_AFTER_CONVERT (disattivato: conserva il TS) e DEDUP_ENABLED (disattivato per default).
- Gestione delle sessioni scadute anche quando EventSource perde autenticazione; il browser torna alla schermata di login preservando il percorso di ritorno.
- Aggiornata la documentazione e la configurazione Docker per la stessa serie di variabili nella produzione e nella Preview.
- Controlli statici e test isolati eseguiti; test end-to-end richiede l'ambiente con Flask e Streamlink.

### Aggiornamento — ricerca, ordinamento e interfaccia incrementale
- Aggiunta la barra di ricerca live per nome e piattaforma, con ordinamento per priorità, live prima, nome, ultima live e ultimo controllo.
- Le schede dei canali vengono create una sola volta e aggiornate campo per campo; gli eventi SSE non ricreano più pulsanti o selettori qualità mentre si interagisce.
- Il riordino automatico viene sospeso durante l'interazione, evitando che un canale si sposti sotto il puntatore o mentre si usa il selettore.
- La modalità “Una volta” rimane attivabile anche da pausa normale e riabilita la registrazione per la live corrente o successiva.
- Cambiando qualità durante REC, il file corrente viene finalizzato e viene preparata una nuova sessione con la qualità scelta; i nomi dei file vengono riservati per evitare sovrascritture dovute a cambi ravvicinati.
- Verificate sintassi Python/JavaScript, YAML Docker Compose e ordinamento/filtraggio con casi isolati. Test end-to-end con Flask/Streamlink/FFmpeg e una live reale non eseguito in questo ambiente.

### Aggiornamento — login opzionale e filtri rapidi
- Aggiunta una pagina di accesso pulita: impostando `APP_PASSWORD` in `.env`, dashboard, API, player live e registrazioni richiedono una sessione autenticata; se `APP_PASSWORD` è vuota l'app resta senza password.
- Le sessioni durano fino a 12 ore, i cookie sono HttpOnly e SameSite=Lax; le richieste di modifica usano un token CSRF e i tentativi di login sono limitati.
- Aggiunte le variabili `APP_PASSWORD`, `SECRET_KEY` e `SESSION_COOKIE_SECURE` a `.env.example` e a Docker Compose.
- Aggiunto il menu filtri: tutti, online, REC, non in pausa, pausa, a dormire, private, offline confermati e stati da verificare.
- Il rilevamento privato si attiva soltanto su messaggi espliciti riconosciuti da Streamlink; lo stato privato è distinto da offline e non aggiorna `last_live`.
- Verificate sintassi Python/JavaScript e YAML Compose. Test HTTP end-to-end non eseguibile perché Flask non è installato nell'ambiente di modifica.

### Aggiornamento — stato online/offline indicato solo dal pallino
- Rimossi i badge testuali “Online” e “Offline” dalle schede: il pallino verde/rosso a sinistra del nome comunica già lo stato.
- Restano visibili gli stati informativi distinti, come “A dormire”, verifica in corso ed errori incerti.
- La logica di monitoraggio e gli stati confermati non sono stati modificati.

### Aggiornamento — pausa normale senza badge ridondante
- Rimossa la dicitura “Pausa · check attivo” dalle schede in pausa normale: il pulsante Play indica già che la registrazione è ferma.
- La pausa normale continua a lasciare attivo il monitor; la modalità “A dormire” conserva il proprio indicatore perché sospende i controlli.

### Aggiornamento — Last live, timestamp ISO-8601 e sigle piattaforma
- Aggiunto `last_live`, aggiornato quando il monitor rileva una risposta online certa; `last_online` resta disponibile come campo compatibile.
- `Last live` è mostrato in forma relativa nell’elenco canali e al passaggio del mouse mostra la data e l’ora complete.
- I timestamp salvati in `channels.json` e nell’export JSON/CSV usano ISO-8601 UTC con suffisso `Z`; i vecchi timestamp Unix vengono convertiti automaticamente al caricamento/salvataggio.
- I badge piattaforma ora mostrano le sigle TW/KI/YT con i colori distintivi di Twitch, Kick e YouTube.
- Un pallino a sinistra del nome indica online (verde), offline confermato (rosso), verifica/errori (giallo) o canale a dormire (viola); lo stato non ancora noto è grigio.
- Sintassi Python/JavaScript e file di configurazione verificati; test su servizi live reali non eseguito in questo ambiente.

### Versione aggiornata — pausa monitorata e modalità A dormire
- La pausa normale ferma la registrazione ma mantiene attivi i controlli della live.
- Aggiunto il pulsante con icona luna: sospende registrazione e monitor del singolo canale.
- Il risveglio riattiva il monitor senza riavviare la registrazione; Play resta esplicito.
- Lo stato `sleeping` è salvato in channels.json ed è incluso in import/export JSON e CSV.
- I controlli manuali e automatici ignorano i canali a dormire; risultati già in volo non possono avviare REC dopo l'attivazione della modalità sonno.
- Rimossa l'ambiguità dell'impostazione globale CHECK_PAUSED_ROOMS, sostituita dal controllo per singolo canale.

### Aggiornamento — flusso registrato normalizzato e preview condivisa corretta
- Durante REC, Streamlink continua ad aprire una sola connessione alla sorgente; FFmpeg rimuxa in modalità copia (`-c copy`) e il medesimo flusso MPEG-TS normalizzato viene scritto nel file e pubblicato al buffer della preview.
- I timestamp vengono riallineati all'inizio di ogni file registrato, evitando che la durata mostrata sia influenzata dal tempo trascorso dall'avvio della live prima di premere REC.
- FFmpeg reinvia le intestazioni MPEG-TS e usa output a pacchetti scaricati, migliorando l'avvio della preview quando si collega a una registrazione già in corso.
- Stop manuale, split per dimensione e interruzione della sorgente terminano sia FFmpeg sia Streamlink; restano invariati audio/video originali senza ricodifica.
- Verificata la sintassi Python e provato il remux con un flusso TS sintetico avente timestamp iniziali distanti: il file risultante parte da timestamp zero e la durata rappresenta solo il contenuto acquisito. Test su live reale non eseguito in questo ambiente.

### Aggiornamento — monitor concorrente, conferma offline e preview condivisa
- Il monitor usa un limite configurabile di worker (default 2) e pianifica ogni canale con intervalli diversi per stato online, offline ed errori; i controlli forzati hanno priorità.
- La fine della live richiede tre risposte offline certe per default; errori/timeout restano stati incerti e non fermano REC. Dopo l'offline confermato viene applicata una breve finestra di tolleranza.
- `last_online`, `last_checked`, stato confermato, conteggio errori e diagnostica vengono salvati in `channels.json`; l'export importa/esporta i timestamp persistenti.
- Durante REC lo stesso flusso Streamlink viene scritto sul file e duplicato in un buffer circolare RAM consumato dalla preview, evitando una seconda connessione alla sorgente.
- A REC ferma, la preview resta indipendente e temporanea; chiudendo il player il sistema ferma FFmpeg e Streamlink e aggiorna il contatore della preview.
- Il pannello mostra stato live/REC/preview, ultimo controllo, ultimo online, conferme offline, errori, tentativi di riconnessione e stato generale del monitor, con comando per controllare subito tutti i canali.
- Nuove variabili: `MONITOR_WORKERS`, `ONLINE_CHECK_INTERVAL`, `OFFLINE_CHECK_INTERVAL`, `ERROR_CHECK_INTERVAL`, `OFFLINE_CONFIRMATIONS`, `OFFLINE_CONFIRM_INTERVAL`, `RECORDER_OFFLINE_GRACE_SECONDS`, `LIVE_STREAM_BUFFER_BYTES`.
- Verificata la sintassi Python e JavaScript e la configurazione YAML; il test con live reali richiede un ambiente con Streamlink, FFmpeg e sorgenti online.

### Aggiornamento — anteprima live indipendente dalla registrazione
- Il pulsante «Guarda live» è sempre disponibile, anche se la registrazione è in pausa o disattivata; se la diretta non è raggiungibile, il player mostra un errore senza creare file.
- L'anteprima non legge più la coda del file `.ts` registrato: apre un flusso temporaneo separato con Streamlink, usando la qualità configurata e lo stesso risolutore del registratore.
- FFmpeg rimuxa il flusso in MPEG-TS con `-c copy`, senza ricodifica e senza creare file su disco.
- Quando il player viene chiuso o la connessione termina, FFmpeg e Streamlink vengono terminati e i relativi descrittori chiusi.
- Verificata la sintassi Python; il test live reale dipende dalla disponibilità di una diretta raggiungibile nell'ambiente di esecuzione.

### Aggiornamento — ambiente Docker Preview separato
- Aggiunto un workflow GitHub Actions dedicato al branch `preview`.
- La build pubblica i tag `preview` e `preview-<commit>` su GHCR senza aggiornare `latest`.
- Aggiunto `docker-compose.preview.yml` con container, porta predefinita 5001 e volume dati separati dalla produzione.
- I pull request verso `preview` verificano la build senza pubblicare immagini.
- Per avviare la preview dopo la pubblicazione dell'immagine:
- `docker compose -f docker-compose.preview.yml pull && docker compose -f docker-compose.preview.yml up -d`

### Aggiornamento — modalità Una volta
- Rinominato il pulsante in "Una volta" e sostituita l'icona con un orologio.
- Attivando la modalità, il canale viene abilitato alla registrazione e la live viene registrata quando risulta online.
- Dopo la fine della diretta, il sistema attende 2 ore; se il canale torna online nel frattempo, il timer si azzera e riparte alla fine della live successiva.
- Se non torna online entro 2 ore, il canale viene messo automaticamente in pausa e la modalità si disattiva: il pulsante torna inattivo.
- Timer e stato della modalità sono salvati insieme al canale, anche in caso di riavvio.
- Una pausa/ripresa manuale o un comando globale pausa/riprendi disattiva la modalità, lasciando prevalere l'azione dell'utente.
- Per aggiornare: ricostruire il container con `docker compose up -d --build`.

### Versione precedente: anteprima dalla registrazione in corso (superata dall’aggiornamento iniziale)
- La preview leggeva dalla coda TS in crescita e compariva solo durante REC.
- L’anteprima live ora è indipendente dalla registrazione e usa un flusso temporaneo diretto.
- Applicazione modifiche dopo la sostituzione dei sorgenti:
- docker compose up -d --build
- Poi ricaricare la pagina del browser.

### Aggiornamento — layout desktop e lista registrazioni stabile
- Su schermi larghi, pannello Canali a sinistra e Registrazioni a destra; su schermi piccoli la disposizione verticale rimane invariata. I due elenchi scorrono indipendentemente.
- La lista registrazioni aggiorna le righe in modo incrementale, senza distruggere e ricreare tutti i nodi DOM a ogni evento SSE; i pulsanti e lo scroll restano stabili.
- L'ordinamento utilizza il timestamp d'inizio incorporato nei nomi generati, quando disponibile, invece del tempo di modifica che cambia continuamente mentre FFmpeg scrive.
- Verificata la sintassi Python e JavaScript. Test di interfaccia in browser non eseguito in questo ambiente.
