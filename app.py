from flask import Flask, render_template, request, redirect, url_for, send_from_directory, flash
import json, os, subprocess, threading, logging, shutil, time
from datetime import datetime
from dotenv import load_dotenv

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
        self.lock = threading.Lock()
        self.monitor_thread = None

    def start(self):
        """Avvia la registrazione se non è già attiva."""
        with self.lock:
            if self.is_recording:
                return
            self.output_path = os.path.join(RECORDINGS_DIR, generate_filename(self.channel_name))
            cmd = ["streamlink", f"https://twitch.tv/{self.channel_name}", STREAM_QUALITY, "-o", self.output_path]
            try:
                self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.is_recording = True
                # Thread che monitora il processo
                threading.Thread(target=self._monitor_process, daemon=True).start()
                # Thread che controlla la dimensione del file
                self.monitor_thread = threading.Thread(target=self._monitor_file_size, daemon=True)
                self.monitor_thread.start()
                logger.info(f"Avviata registrazione: {self.channel_name}")
            except Exception as e:
                logger.error(f"Errore avvio registrazione {self.channel_name}: {e}")

    def _monitor_process(self):
        """Monitora il processo streamlink e cattura eventuali errori."""
        if self.process:
            _, stderr = self.process.communicate()
            if stderr:
                logger.error(f"Errore registrazione {self.channel_name}: {stderr.decode(errors='ignore')}")
            with self.lock:
                self.is_recording = False

    def _monitor_file_size(self):
        """Controlla che il file non superi la dimensione massima, altrimenti lo divide."""
        while self.is_recording and self.process:
            try:
                if os.path.exists(self.output_path):
                    size = os.path.getsize(self.output_path)
                    if size >= MAX_FILE_SIZE:
                        logger.info(f"File {self.output_path} ha raggiunto {size} bytes, creando nuovo file...")
                        self._split_recording()
                        break
            except Exception as e:
                logger.error(f"Errore controllo dimensione file {self.channel_name}: {e}")
            time.sleep(5)  # controlla ogni 5 secondi

    def _split_recording(self):
        """Ferma la registrazione e la riavvia su un nuovo file."""
        self.stop()
        time.sleep(1)  # breve pausa per sicurezza
        self.start()

    def stop(self):
        """Ferma la registrazione in corso."""
        with self.lock:
            if self.process and self.is_recording:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                logger.info(f"Registrazione interrotta: {self.channel_name}")
            self.process = None
            self.is_recording = False

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
        for ch in channels:
            name = ch['name']
            ch['online'] = is_channel_online(name)
            rec = recorders.get(name)

            if ch.get('is_recording', False):
                if ch['online'] and not rec.is_recording:
                    rec.start()
                if not ch['online'] and rec.is_recording:
                    rec.stop()
            else:
                if rec.is_recording:
                    rec.stop()

        save_channels(channels)  # Salva lo stato aggiornato
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

        # --- Aggiungi canale ---
        if action == 'add' and channel_name:
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
            ch = next((c for c in channels if c['name'] == channel_name), None)
            if ch is None:
                flash("Canale non trovato", "danger")
            else:
                rec = recorders.get(channel_name)
                if action == 'pause':
                    ch['is_recording'] = False
                    save_channels(channels)
                    if rec and rec.is_recording:
                        rec.stop()
                    flash(f"Registrazione di {channel_name} messa in pausa.", "info")
                else:  # resume
                    ch['is_recording'] = True
                    save_channels(channels)
                    if rec and ch.get('online', False) and not rec.is_recording:
                        rec.start()
                    flash(f"Registrazione di {channel_name} ripresa (in attesa se offline).", "success")

        # --- Rimuovi canale ---
        elif action == 'remove' and channel_name in recorders:
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

# AVVIO SERVER
if __name__ == '__main__':
    # Avvia subito la registrazione per i canali già attivi e online
    for ch in channels:
        if ch.get('is_recording', False) and ch.get('online', False):
            recorders[ch['name']].start()
    app.run(host='0.0.0.0', port=PORT)
