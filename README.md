# Twitch Recorder

Un’applicazione web (Flask + Bootstrap) per registrare automaticamente i canali **Twitch** quando sono online.  
Supporta più canali contemporaneamente, divisione automatica dei file grandi e interfaccia web intuitiva per gestire i canali e scaricare le registrazioni.

---

## Funzionalità

- Aggiungi più canali Twitch da interfaccia web  
- Rilevamento automatico **online/offline** dei canali  
- Avvio/pausa/ripresa della registrazione con un click  
- Suddivisione automatica dei file quando raggiungono una dimensione massima configurabile  
- Interfaccia web responsive con **Bootstrap 5** e **tema dark/light**  
- Scarica o apri direttamente i file registrati dal browser  
- Salvataggio stato canali in file JSON  
- Supporto a **Docker**  

---

## Anteprima

### Canali
![channels](https://github.com/user-attachments/assets/4ef69d2a-2721-4d02-8454-f10923ee8b5f)

### Registrazioni
![recordings](https://github.com/user-attachments/assets/41280437-0ff2-4c0e-88a5-2eeb29139340)

---

## Requisiti

- [Python 3.9+](https://www.python.org/)  
- [streamlink](https://streamlink.github.io/) (necessario per registrare gli stream)  
- Twitch account **non necessario**, basta il nome del canale pubblico.  

---

## Installazione

### Clona il repository
```bash
git clone https://github.com/alessandromasone/twitch-recorder.git
cd twitch-recorder
````

### Installa le dipendenze

Consiglio un virtualenv:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Configura il file `.env`

Crea un file `.env` nella root del progetto (o copia `.env.example` se disponibile):

```ini
CHANNELS_FILE=channels.json
RECORDINGS_DIR=recordings
STREAM_QUALITY=best
CHECK_INTERVAL=60
PORT=5000
MAX_FILE_SIZE=1932735283  # 1.8 GB circa
```

### Avvia l’app

```bash
python app.py
```

Poi apri il browser su:
[http://localhost:5000](http://localhost:5000)

---

## Uso con Docker

### Build dell’immagine

```bash
docker build -t twitch-recorder .
```

### Avvio del container

```bash
docker run -d \
  --name twitch-recorder \
  -p 5000:5000 \
  -v $(pwd)/recordings:/app/recordings \
  -v $(pwd)/channels.json:/app/channels.json \
  twitch-recorder
```

* La cartella `recordings` conterrà i video scaricati (montata come volume)
* `channels.json` mantiene la lista dei canali da un avvio all’altro

### Variabili d’ambiente

Puoi sovrascrivere la configurazione con `-e`, ad esempio:

```bash
docker run -d \
  -p 8080:5000 \
  -e STREAM_QUALITY=720p \
  -e CHECK_INTERVAL=30 \
  twitch-recorder
```

---

## Utilizzo

1. Apri l’interfaccia web
2. Inserisci il nome di un canale Twitch e premi **Aggiungi e registra**
3. L’app controllerà periodicamente lo stato del canale:

   * Se è **online** → avvia la registrazione
   * Se va **offline** → ferma la registrazione
4. Gestisci i canali con i pulsanti **Pausa / Riprendi / Rimuovi**
5. Scarica i file dalla sezione **Registrazioni**
