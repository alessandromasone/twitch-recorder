from flask import Flask, render_template, request, redirect, url_for, send_from_directory, flash
import json, os, subprocess, threading, logging, shutil, time, copy, concurrent.futures, signal, sys
from datetime import datetime
from dotenv import load_dotenv
import secrets

# CARICAMENTO CONFIGURAZIONE
# Carica variabili d'ambiente dal file .env (se presente)
load_dotenv()

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)  # nuova chiave random a ogni avvio

# --- CONFIGURAZIONE ---
CHANNELS_FILE   = os.getenv("CHANNELS_FILE", "channels.json")     # File JSON dove vengono salvati i canali
RECORDINGS_DIR  = os.getenv("RECORDINGS_DIR", "recordings")       # Cartella di destinazione delle registrazioni
FILE_EXTENSION  = os.getenv("FILE_EXTENSION", ".ts")              # Estensione file video
FILENAME_FORMAT = os.getenv("FILENAME_FORMAT", "{name}_{timestamp}{ext}")  # Formato del nome file
STREAM_QUALITY  = os.getenv("STREAM_QUALITY", "best")             # Qualità stream (parametro di streamlink)
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", 60))            # Intervallo di monitoraggio canali (secondi)
PORT            = int(os.getenv("PORT", 5000))                    # Porta del server Flask
MAX_FILE_SIZE   = float(os.getenv("MAX_FILE_SIZE", 1.8 * 1024 * 1024 * 1024))  # Dimensione massima file (default 1.8GB)

# LOGGING
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# LOCK GLOBALE PER THREAD SAFETY
data_lock = threading.Lock()

# PREPARAZIONE CARTELLE
os.makedirs(RECORDINGS_DIR, exist_ok=True)  # Crea la cartella delle registrazioni se non esiste

# FUNZIONI UTILI
def generate_filename(channel_name):
    """Genera un nome file basato sul formato configurato e timestamp corrente."""
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return FILENAME_FORMAT.format(name=channel_name, timestamp=ts, ext=FILE_EXTENSION)

def load_channels():
    """Carica la lista dei canali dal file JSON."""
    if os.path.exists(CHANNELS_FILE):
        with open(CHANNELS_FILE) as f:
            return json.load(f)
    return []

def save_channels(channels):
    """Salva la lista dei canali sul file JSON."""
    with open(CHANNELS_FILE, 'w') as f:
        json.dump(channels, f, indent=2)

def is_channel_online(channel_name):
    """Verifica se il canale Twitch è online tramite streamlink."""
    try:
        result = subprocess.run(
            ["streamlink", f"https://twitch.tv/{channel_name}", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10
        )
        return result.returncode == 0 and b"streams" in result.stdout
    except Exception:
        return False

# CLASSE RECORDER
class Recorder:
    """
    Classe che gestisce la registrazione di un singolo canale Twitch.
    Si occupa di avviare/fermare streamlink, monitorare il processo e
    gestire la divisione automatica dei file se troppo grandi.
    """
    def __init__(self, channel_name):
        self.channel_name = channel_name
        self.process = None
        self.output_path = None
        self.is_recording = False
        self.stop_requested = False
        self.lock = threading.Lock()
        self.manager_thread = None
        self.log_file = None

    def start(self):
        """Avvia la registrazione in background (con gestione riavvii)."""
        with self.lock:
            if self.is_recording:
                return
            self.stop_requested = False
            self.is_recording = True
            self.manager_thread = threading.Thread(target=self._recording_manager, daemon=True)
            self.manager_thread.start()
            logger.info(f"Avviata registrazione: {self.channel_name}")

    def _recording_manager(self):
        """Gestisce il ciclo di vita della registrazione, riavviando in caso di errori o split."""
        error_start_time = None

        while not self.stop_requested:
            self.output_path = os.path.join(RECORDINGS_DIR, generate_filename(self.channel_name))
            cmd = ["streamlink", f"https://twitch.tv/{self.channel_name}", STREAM_QUALITY, "-o", self.output_path]
            
            log_path = os.path.join(RECORDINGS_DIR, f"{self.channel_name}.log")
            process_start_time = time.time()

            try:
                self.log_file = open(log_path, "a")
                self.process = subprocess.Popen(cmd, stdout=self.log_file, stderr=self.log_file)
                
                # Monitor dimensione file dedicato a questo processo
                size_monitor = threading.Thread(target=self._monitor_file_size, args=(self.process, self.output_path), daemon=True)
                size_monitor.start()

                self.process.wait()
            except Exception as e:
                logger.error(f"Errore streamlink {self.channel_name}: {e}")
            finally:
                if self.log_file:
                    self.log_file.close()
                    self.log_file = None
                self.process = None

            if self.stop_requested:
                break

            # Logica riavvio / tolleranza errori
            duration = time.time() - process_start_time
            if duration < 20:
                # Se il processo è durato poco, consideriamo errore
                if error_start_time is None:
                    error_start_time = time.time()
                
                if time.time() - error_start_time > 180:
                    logger.error(f"Superata tolleranza errori (180s) per {self.channel_name}. Stop.")
                    break
                
                logger.warning(f"Errore/Crash {self.channel_name}. Riavvio tra 5s...")
                time.sleep(5)
            else:
                # Reset timer se il processo è durato a lungo
                error_start_time = None
                logger.info(f"Processo terminato (split o fine). Riavvio immediato {self.channel_name}.")
                time.sleep(1)

        with self.lock:
            self.is_recording = False

    def _monitor_file_size(self, current_process, path):
        """Controlla dimensione file e termina il processo se necessario."""
        while not self.stop_requested:
            if current_process.poll() is not None:
                break
            try:
                if os.path.exists(path):
                    size = os.path.getsize(path)
                    if size >= MAX_FILE_SIZE:
                        logger.info(f"File {path} pieno. Split...")
                        current_process.terminate()
                        break
            except Exception as e:
                pass
            time.sleep(5)  # controlla ogni 5 secondi

    def stop(self):
        """Ferma la registrazione in corso."""
        with self.lock:
            if not self.is_recording:
                return
            self.stop_requested = True
            if self.process:
                try:
                    self.process.terminate()
                except Exception:
                    pass
            logger.info(f"Stop richiesto: {self.channel_name}")

# GLOBAL: CANALI E RECORDER
channels  = load_channels()  # Carica canali dal file JSON
recorders = {ch['name']: Recorder(ch['name']) for ch in channels}  # Crea un Recorder per ogni canale

# THREAD MONITOR
def monitor_channels():
    """
    Thread che ciclicamente controlla lo stato dei canali:
    - Avvia la registrazione se online e attivata
    - Ferma la registrazione se offline o disattivata
    """
    while True:
        try:
            # Copia la lista per non bloccare il lock durante il check online (che è lento)
            with data_lock:
                channels_copy = copy.deepcopy(channels)

            # Check online status (senza lock)
            online_status = {}
            # Controllo parallelo per velocizzare il monitoraggio (max 5 check contemporanei)
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_to_name = {executor.submit(is_channel_online, ch['name']): ch['name'] for ch in channels_copy}
                for future in concurrent.futures.as_completed(future_to_name):
                    name = future_to_name[future]
                    # is_channel_online gestisce già le eccezioni internamente
                    online_status[name] = future.result()

            # Applica modifiche e gestisci registrazioni (con lock)
            with data_lock:
                for ch in channels:
                    name = ch['name']
                    # Aggiorna stato online
                    ch['online'] = online_status.get(name, False)
                    
                    rec = recorders.get(name)
                    if ch.get('is_recording', False):
                        if ch['online'] and not rec.is_recording:
                            rec.start()
                        if not ch['online'] and rec.is_recording:
                            rec.stop()
                    else:
                        if rec.is_recording:
                            rec.stop()
                save_channels(channels)
        except Exception as e:
            logger.error(f"Errore nel ciclo di monitoraggio: {e}")
        
        time.sleep(CHECK_INTERVAL)

# Avvia il thread in background
threading.Thread(target=monitor_channels, daemon=True).start()

# ROTTE FLASK
@app.route('/', methods=['GET', 'POST'])
def index():
    """
    Homepage con:
    - Lista canali monitorati
    - Azioni: aggiungi, pausa, riprendi, rimuovi
    - Elenco registrazioni salvate
    """
    global channels
    if request.method == 'POST':
        action = request.form.get('action')
        channel_name = request.form.get('channel', '').strip().lower()

        # Gestione input URL: estrae il nome canale se viene incollato un link completo
        if "twitch.tv/" in channel_name:
            channel_name = channel_name.split("twitch.tv/")[-1].split("/")[0].split("?")[0]

        # --- Aggiungi canale ---
        if action == 'add' and channel_name:
            with data_lock:
                if not any(ch['name'] == channel_name for ch in channels):
                    ch_info = {"name": channel_name, "is_recording": True, "online": False}
                    channels.append(ch_info)
                    save_channels(channels)
                    recorders[channel_name] = Recorder(channel_name)
                    flash(f"Canale {channel_name} aggiunto e in attesa di registrazione.", "success")
                else:
                    flash("Canale già presente.", "warning")

        # --- Pausa / Riprendi ---
        elif action in ('pause', 'resume') and channel_name:
            with data_lock:
                ch = next((c for c in channels if c['name'] == channel_name), None)
                if ch is None:
                    flash("Canale non trovato", "danger")
                else:
                    rec = recorders.get(channel_name)
                    if action == 'pause':
                        ch['is_recording'] = False
                        if rec and rec.is_recording:
                            rec.stop()
                        flash(f"Registrazione di {channel_name} messa in pausa.", "info")
                    else:  # resume
                        ch['is_recording'] = True
                        # Il monitor thread lo avvierà al prossimo ciclo se online
                        flash(f"Registrazione di {channel_name} ripresa (in attesa se offline).", "success")
                    save_channels(channels)

        # --- Rimuovi canale ---
        elif action == 'remove' and channel_name in recorders:
            with data_lock:
                recorders[channel_name].stop()
                del recorders[channel_name]
                channels = [c for c in channels if c['name'] != channel_name]
                save_channels(channels)
                flash(f"Canale {channel_name} rimosso.", "danger")

        return redirect(url_for('index'))

    # --- Info spazio libero ---
    total, used, free = shutil.disk_usage(RECORDINGS_DIR)
    free_space = f"{free // (1024*1024*1024)} GB liberi"

    # --- Lista registrazioni esistenti ---
    recordings = sorted(os.listdir(RECORDINGS_DIR), reverse=True)

    return render_template('index.html', channels=channels, recorders=recorders,
                           recordings=recordings, free_space=free_space)

@app.route('/recordings/<path:filename>')
def download_recording(filename):
    """Permette di scaricare le registrazioni dalla cartella RECORDINGS_DIR."""
    return send_from_directory(RECORDINGS_DIR, filename)

@app.route('/delete_recording', methods=['POST'])
def delete_recording():
    """Elimina una registrazione specifica."""
    filename = request.form.get('filename')
    if filename:
        # Sicurezza: usa basename per evitare path traversal (es. ../../windows)
        safe_filename = os.path.basename(filename)
        file_path = os.path.join(RECORDINGS_DIR, safe_filename)
        
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                flash(f"File {safe_filename} eliminato con successo.", "success")
            except Exception as e:
                logger.error(f"Errore eliminazione file {safe_filename}: {e}")
                flash(f"Errore durante l'eliminazione: {e}", "danger")
        else:
            flash("File non trovato.", "warning")
            
    return redirect(url_for('index'))

# GESTIONE CHIUSURA (Graceful Shutdown)
def signal_handler(sig, frame):
    logger.info("Ricevuto segnale di stop. Chiusura registrazioni in corso...")
    # Usa list() per evitare errori se il dizionario cambia durante l'iterazione
    for name, rec in list(recorders.items()):
        if rec.is_recording:
            rec.stop()
    sys.exit(0)

# AVVIO SERVER
if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    app.run(host='0.0.0.0', port=PORT)
