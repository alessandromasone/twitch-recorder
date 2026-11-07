"""Configurazione letta dalle variabili d'ambiente (compatibile con le versioni precedenti)."""

import logging
import os

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


APP_VERSION = os.getenv("APP_VERSION", "dev").strip() or "dev"
CHANNELS_FILE = os.getenv("CHANNELS_FILE", "channels.json")
DATA_DIR = os.path.dirname(os.path.abspath(CHANNELS_FILE)) or os.getcwd()
RECORDINGS_DIR = os.getenv("RECORDINGS_DIR", "recordings")
FILE_EXTENSION = os.getenv("FILE_EXTENSION", ".ts")
FILENAME_FORMAT = os.getenv("FILENAME_FORMAT", "{name}_{timestamp}{ext}")
STREAM_QUALITY = os.getenv("STREAM_QUALITY", "best")
CHECK_INTERVAL = _env_int("CHECK_INTERVAL", 60)
SINGLE_LIVE_GRACE_SECONDS = 2 * 60 * 60
PORT = _env_int("PORT", 5000)
MAX_FILE_SIZE = _env_int("MAX_FILE_SIZE", int(1.8 * 1024 ** 3))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ── Accesso ──
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
LOGIN_ENABLED = bool(APP_PASSWORD)
SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE")
APP_SESSION_HOURS = max(1, min(24 * 30, _env_int("APP_SESSION_HOURS", 12)))

# ── Twitch Helix e rerun ──
SKIP_RERUNS = _env_bool("SKIP_RERUNS")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
# Metodo di controllo per Twitch: auto (Helix se ci sono le credenziali), helix, streamlink
TWITCH_CHECK_METHOD = os.getenv("TWITCH_CHECK_METHOD", "auto").strip().lower()
HELIX_AVAILABLE = bool(TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET)
HELIX_ENABLED = HELIX_AVAILABLE and TWITCH_CHECK_METHOD in ("auto", "helix")
# Con Helix un controllo costa una sola richiesta HTTP per 100 canali: si può controllare
# più spesso i canali offline senza appesantire il sistema.
HELIX_OFFLINE_CHECK_INTERVAL = max(10, int(os.getenv("HELIX_OFFLINE_CHECK_INTERVAL", "20") or 20))
# Rilevamento rerun non dichiarate: politica predefinita per i canali senza scelta esplicita.
#   record = ignora i sospetti, mark = registra e segna, skip = non registrare se probabile
RERUN_DEFAULT_POLICY = os.getenv("RERUN_DEFAULT_POLICY", "mark").strip().lower()
if RERUN_DEFAULT_POLICY not in ("record", "mark", "skip"):
    RERUN_DEFAULT_POLICY = "mark"
RERUN_SUSPECT_SCORE = max(10, min(95, _env_int("RERUN_SUSPECT_SCORE", 40)))
RERUN_PROBABLE_SCORE = max(RERUN_SUSPECT_SCORE + 5, min(100, _env_int("RERUN_PROBABLE_SCORE", 70)))
SKIP_TWITCH_ADS = _env_bool("SKIP_TWITCH_ADS", "true")

# ── Spazio su disco ──
# Sotto questa soglia di spazio libero (GB) non vengono avviate nuove registrazioni.
# Quelle già in corso continuano. 0 disattiva il controllo.
try:
    MIN_FREE_GB = max(0.0, float(os.getenv("MIN_FREE_GB", "2")))
except ValueError:
    MIN_FREE_GB = 2.0

# ── Elaborazione file ──
AUTO_CONVERT = _env_bool("AUTO_CONVERT")
DELETE_AFTER_CONVERT = _env_bool("DELETE_AFTER_CONVERT")
DEDUP_ENABLED = _env_bool("DEDUP_ENABLED")
DEDUP_BYTES = max(64 * 1024, _env_int("DEDUP_BYTES", 2 * 1024 * 1024))
DEDUP_FILE = os.getenv("DEDUP_FILE", "").strip() or os.path.join(DATA_DIR, "dedup_hashes.json")
HISTORY_FILE = os.getenv("HISTORY_FILE", "").strip() or os.path.join(DATA_DIR, "channel_history.json")
# Titolo, categoria e rerun di ogni file: accanto a channels.json, non tra i video.
RECORDINGS_META_FILE = os.path.join(DATA_DIR, "recordings_meta.json")
# Percorsi usati fino alla 2.0: vengono migrati o rimossi all'avvio.
LEGACY_META_FILE = os.path.join(RECORDINGS_DIR, ".recordings_meta.json")
LEGACY_THUMBS_DIR = os.path.join(RECORDINGS_DIR, ".thumbs")
# Miniature dei file conclusi tenute solo in memoria (non vengono mai salvate su disco).
RECORDING_THUMBS_CACHE = 300
# Log di Streamlink/FFmpeg per canale (recordings/.<canale>.log). false = nessun file di log.
RECORDING_LOGS = _env_bool("RECORDING_LOGS", "true")
# Oltre questa dimensione il log viene ruotato in .<canale>.log.1 (al massimo due file per canale).
try:
    RECORDING_LOG_MAX_MB = max(0.1, float(os.getenv("RECORDING_LOG_MAX_MB", "5")))
except ValueError:
    RECORDING_LOG_MAX_MB = 5.0
# Interfaccia minimal: lista densa di solo testo, senza copertine, avatar e animazioni.
# Ogni browser può comunque cambiarla dalle impostazioni del pannello.
MINIMAL_UI = _env_bool("MINIMAL_UI")
RECORDING_THUMBS = _env_bool("RECORDING_THUMBS", "false" if MINIMAL_UI else "true") \
    if os.getenv("RECORDING_THUMBS", "").strip() else not MINIMAL_UI

# ── Monitor ──
MONITOR_WORKERS = max(1, min(8, _env_int("MONITOR_WORKERS", 2)))
OFFLINE_CONFIRMATIONS = max(2, min(5, _env_int("OFFLINE_CONFIRMATIONS", 3)))
OFFLINE_CONFIRM_INTERVAL = max(2, _env_int("OFFLINE_CONFIRM_INTERVAL", 5))
ONLINE_CHECK_INTERVAL = max(10, _env_int("ONLINE_CHECK_INTERVAL", 30))
OFFLINE_CHECK_INTERVAL = max(10, _env_int("OFFLINE_CHECK_INTERVAL", max(30, CHECK_INTERVAL)))
ERROR_CHECK_INTERVAL = max(10, _env_int("ERROR_CHECK_INTERVAL", 120))
RECORDER_OFFLINE_GRACE_SECONDS = max(0, _env_int("RECORDER_OFFLINE_GRACE_SECONDS", 15))
LIVE_STREAM_BUFFER_BYTES = max(512 * 1024, _env_int("LIVE_STREAM_BUFFER_BYTES", 4 * 1024 * 1024))
PREVIEW_STARTUP_TIMEOUT_SECONDS = max(30, min(600, _env_int("PREVIEW_STARTUP_TIMEOUT_SECONDS", 120)))
PREVIEW_IDLE_TIMEOUT_SECONDS = max(30, min(900, _env_int("PREVIEW_IDLE_TIMEOUT_SECONDS", 180)))
# Dopo che l'ultimo spettatore chiude il player, la sorgente temporanea resta aperta
# per qualche secondo: riaprire subito il player non crea una nuova connessione.
PREVIEW_LINGER_SECONDS = max(0, min(120, _env_int("PREVIEW_LINGER_SECONDS", 10)))

JITTER_MAX = _env_int("JITTER_MAX", 10)
RECONNECT_WAIT = _env_int("RECONNECT_WAIT", 30)
RECONNECT_TRIES = _env_int("RECONNECT_TRIES", 3)

# ── Notifiche ──
NOTIFY_TYPE = os.getenv("NOTIFY_TYPE", "").lower()
NOTIFY_URL = os.getenv("NOTIFY_URL", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
NOTIFY_EVENTS = set(os.getenv("NOTIFY_EVENTS", "online,offline,rec_start,rec_end").split(","))
TELEGRAM_PHOTO = os.getenv("TELEGRAM_NOTIFY_PHOTO", "true").lower() in ("1", "true", "yes", "on")

QUALITY_OPTIONS = [
    "best", "1080p60", "1080p", "720p60", "720p",
    "480p", "360p", "160p", "worst", "audio_only",
]
VIDEO_EXTS = {".ts", ".mp4", ".mkv", ".flv", ".avi", ".mov", ".webm"}

DEFAULT_PLATFORM = "twitch"
PLATFORMS: dict[str, dict] = {
    "twitch": {"label": "Twitch", "short": "TW", "color": "#9146FF", "url": "https://twitch.tv/{name}"},
    "kick": {"label": "Kick", "short": "KI", "color": "#53FC18", "url": "https://kick.com/{name}"},
    "youtube": {"label": "YouTube", "short": "YT", "color": "#FF0033", "url": "https://www.youtube.com/@{name}/live"},
}

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="[%(asctime)s] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("twitch-recorder")

os.makedirs(RECORDINGS_DIR, exist_ok=True)
