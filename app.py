"""
Twitch Recorder — Backend Flask
Registra automaticamente stream Twitch quando vanno online.
"""

import csv
import hashlib
import hmac
import io
import json
import os
import re
import subprocess
import threading
import logging
import shutil
import time
import copy
import signal
import sys
import secrets
import mimetypes
import random
import queue
import select
import urllib.request
import urllib.error
import urllib.parse
from collections import deque
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect,
    url_for, send_from_directory, flash, jsonify,
    Response, abort, session,
)
from dotenv import load_dotenv

# ─── Configurazione ──────────────────────────────────────────────────────────

load_dotenv()

CHANNELS_FILE   = os.getenv("CHANNELS_FILE", "channels.json")
RECORDINGS_DIR  = os.getenv("RECORDINGS_DIR", "recordings")
FILE_EXTENSION  = os.getenv("FILE_EXTENSION", ".ts")
FILENAME_FORMAT = os.getenv("FILENAME_FORMAT", "{name}_{timestamp}{ext}")
STREAM_QUALITY  = os.getenv("STREAM_QUALITY", "best")
CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", "60"))
SINGLE_LIVE_GRACE_SECONDS = 2 * 60 * 60  # attesa di 2 ore dopo la fine confermata della diretta
PORT            = int(os.getenv("PORT", "5000"))
MAX_FILE_SIZE   = int(os.getenv("MAX_FILE_SIZE", str(int(1.8 * 1024**3))))
LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO").upper()
APP_PASSWORD    = os.getenv("APP_PASSWORD", "")
LOGIN_ENABLED   = bool(APP_PASSWORD)
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").strip().lower() in ("1", "true", "yes", "on")
APP_SESSION_HOURS = max(1, min(24 * 30, int(os.getenv("APP_SESSION_HOURS", "12"))))

# Rilevamento facoltativo delle rerun Twitch tramite tag ufficiale e titolo.
SKIP_RERUNS = os.getenv("SKIP_RERUNS", "false").strip().lower() in ("1", "true", "yes", "on")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
# Streamlink >= 7.5 filtra già automaticamente gli annunci incorporati su Twitch.
# La variabile abilita l'opzione esplicita solo sulle versioni che ancora la supportano.
SKIP_TWITCH_ADS = os.getenv("SKIP_TWITCH_ADS", "true").strip().lower() in ("1", "true", "yes", "on")

# Funzioni opzionali per le registrazioni concluse. Entrambe sono disattivate di default.
AUTO_CONVERT = os.getenv("AUTO_CONVERT", "false").strip().lower() in ("1", "true", "yes", "on")
DELETE_AFTER_CONVERT = os.getenv("DELETE_AFTER_CONVERT", "false").strip().lower() in ("1", "true", "yes", "on")
DEDUP_ENABLED = os.getenv("DEDUP_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
DEDUP_BYTES = max(64 * 1024, int(os.getenv("DEDUP_BYTES", str(2 * 1024 * 1024))))
DEDUP_FILE = os.getenv("DEDUP_FILE", "").strip() or os.path.join(
    os.path.dirname(os.path.abspath(CHANNELS_FILE)), "dedup_hashes.json"
)

# ── Monitor adattivo e concorrente ──
MONITOR_WORKERS = max(1, min(8, int(os.getenv("MONITOR_WORKERS", "2"))))
OFFLINE_CONFIRMATIONS = max(2, min(5, int(os.getenv("OFFLINE_CONFIRMATIONS", "3"))))
OFFLINE_CONFIRM_INTERVAL = max(2, int(os.getenv("OFFLINE_CONFIRM_INTERVAL", "5")))
ONLINE_CHECK_INTERVAL = max(10, int(os.getenv("ONLINE_CHECK_INTERVAL", "30")))
OFFLINE_CHECK_INTERVAL = max(10, int(os.getenv("OFFLINE_CHECK_INTERVAL", str(max(30, CHECK_INTERVAL)))))
ERROR_CHECK_INTERVAL = max(10, int(os.getenv("ERROR_CHECK_INTERVAL", "120")))
RECORDER_OFFLINE_GRACE_SECONDS = max(0, int(os.getenv("RECORDER_OFFLINE_GRACE_SECONDS", "15")))
LIVE_STREAM_BUFFER_BYTES = max(512 * 1024, int(os.getenv("LIVE_STREAM_BUFFER_BYTES", str(4 * 1024 * 1024))))

# ── Anti-spam ──
JITTER_MAX         = int(os.getenv("JITTER_MAX", "10"))

# ── Retry intelligente ──
RECONNECT_WAIT  = int(os.getenv("RECONNECT_WAIT", "30"))
RECONNECT_TRIES = int(os.getenv("RECONNECT_TRIES", "3"))

# ── Notifiche ──
NOTIFY_TYPE     = os.getenv("NOTIFY_TYPE", "").lower()
NOTIFY_URL      = os.getenv("NOTIFY_URL", "")
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")
NOTIFY_EVENTS   = set(os.getenv("NOTIFY_EVENTS", "online,offline,rec_start,rec_end").split(","))
TELEGRAM_PHOTO  = os.getenv("TELEGRAM_NOTIFY_PHOTO", "true").lower() in ("1", "true", "yes", "on")

# Qualità disponibili in ordine decrescente
QUALITY_OPTIONS = [
    "best", "1080p60", "1080p", "720p60", "720p",
    "480p", "360p", "160p", "worst", "audio_only",
]

_VIDEO_EXTS = {".ts", ".mp4", ".mkv", ".flv", ".avi", ".mov", ".webm"}

# ── Piattaforme supportate (via streamlink) ─────────────────────────────────
# {name} = nome del canale. La registrazione usa sempre streamlink → file .ts.
DEFAULT_PLATFORM = "twitch"
PLATFORMS: dict[str, dict] = {
    "twitch":  {"label": "Twitch",  "short": "TW", "color": "#9146FF", "url": "https://twitch.tv/{name}"},
    "kick":    {"label": "Kick",    "short": "KI", "color": "#53FC18", "url": "https://kick.com/{name}"},
    "youtube": {"label": "YouTube", "short": "YT", "color": "#FF0033", "url": "https://www.youtube.com/@{name}/live"},
}


def _norm_platform(platform: str | None) -> str:
    p = (platform or "").strip().lower()
    return p if p in PLATFORMS else DEFAULT_PLATFORM


def _platform_url(platform: str, name: str) -> str:
    p = PLATFORMS.get(_norm_platform(platform), PLATFORMS[DEFAULT_PLATFORM])
    return p["url"].format(name=name)


def _clean_name(platform: str, raw: str) -> str:
    """Estrae e ripulisce il nome canale (anche se viene incollato un URL)."""
    raw = (raw or "").strip()
    for host in ("twitch.tv/", "kick.com/", "youtube.com/"):
        if host in raw:
            raw = raw.split(host)[-1]
            break
    raw = raw.split("?")[0].split("#")[0]
    if "/" in raw:
        segs = [s for s in raw.rstrip("/").split("/") if s and s != "live"]
        if segs:
            raw = segs[-1]
    raw = raw.lstrip("@")
    name = re.sub(r"[^A-Za-z0-9_.-]", "", raw)
    if _norm_platform(platform) in ("twitch", "kick"):
        name = name.lower()
    return name


def _platform_of(channel: str) -> str:
    for ch in _channels:
        if ch.get("name") == channel:
            return _norm_platform(ch.get("platform"))
    return DEFAULT_PLATFORM

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="[%(asctime)s] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("twitch-recorder")

_twitch_token_lock = threading.Lock()
_twitch_app_token = ""
_twitch_app_token_expires = 0.0
_twitch_meta_lock = threading.Lock()
# login -> (expires_at, state, stream_metadata, error); keeps Helix requests bounded.
_twitch_meta_cache: dict[str, tuple[float, str, dict | None, str | None]] = {}
_twitch_meta_cache_ttl = 20.0
_streamlink_legacy_ads_lock = threading.Lock()
_streamlink_legacy_ads_supported: bool | None = None
if SKIP_RERUNS and not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET):
    logger.warning(
        "SKIP_RERUNS=true ma TWITCH_CLIENT_ID/TWITCH_CLIENT_SECRET non sono configurati: "
        "il monitor non potrà confermare le live Twitch e non avvierà nuove registrazioni quando il controllo rerun è incerto."
    )
elif SKIP_RERUNS:
    logger.info("Esclusione rerun Twitch attiva: verifica dei tag live tramite Helix")

# ─── Flask app ────────────────────────────────────────────────────────────────

app = Flask(__name__)


def _load_persistent_secret_key() -> str:
    """Usa SECRET_KEY se configurata; altrimenti conserva una chiave accanto a channels.json."""
    configured = os.getenv("SECRET_KEY", "").strip()
    if configured:
        return configured

    secret_dir = os.path.dirname(os.path.abspath(CHANNELS_FILE)) or os.getcwd()
    secret_path = os.path.join(secret_dir, ".twitch-recorder-session-secret")
    try:
        os.makedirs(secret_dir, exist_ok=True)
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = None
        if fd is not None:
            with os.fdopen(fd, "w", encoding="utf-8") as secret_file:
                secret_file.write(secrets.token_urlsafe(48))
        with open(secret_path, "r", encoding="utf-8") as secret_file:
            value = secret_file.read().strip()
        if value:
            try:
                os.chmod(secret_path, 0o600)
            except OSError:
                pass
            return value
    except OSError as exc:
        logger.warning("Impossibile salvare la chiave di sessione persistente: %s", exc)
    # Ultima risorsa per installazioni in sola lettura: le sessioni si invalideranno al riavvio.
    return secrets.token_urlsafe(48)


app.secret_key = _load_persistent_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=APP_SESSION_HOURS),
)

os.makedirs(RECORDINGS_DIR, exist_ok=True)

# ── Autenticazione opzionale ──
_login_attempts: dict[str, deque[float]] = {}
_login_attempts_lock = threading.Lock()
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_ATTEMPTS = 5


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def _template_security_context():
    return {
        "csrf_token_value": _csrf_token(),
        "login_enabled": LOGIN_ENABLED,
        "session_hours": APP_SESSION_HOURS,
    }


def _login_wait_seconds(ip: str) -> int:
    now = time.time()
    with _login_attempts_lock:
        attempts = _login_attempts.setdefault(ip, deque())
        while attempts and now - attempts[0] >= _LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        return max(0, int(_LOGIN_WINDOW_SECONDS - (now - attempts[0]))) if len(attempts) >= _LOGIN_MAX_ATTEMPTS else 0


def _record_login_failure(ip: str) -> None:
    now = time.time()
    with _login_attempts_lock:
        attempts = _login_attempts.setdefault(ip, deque())
        while attempts and now - attempts[0] >= _LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        attempts.append(now)


def _safe_local_next(target: str | None) -> str:
    target = target or "/"
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


@app.before_request
def _require_login_and_csrf():
    if not LOGIN_ENABLED or request.endpoint in ("login", "static", "health"):
        return None

    if not session.get("authenticated"):
        if (request.path.startswith(("/api/", "/live/", "/recordings/")) or request.is_json
                or request.headers.get("Accept", "").startswith("application/json")):
            return jsonify({"ok": False, "login_required": True, "error": "Autenticazione richiesta"}), 401
        next_path = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=next_path))

    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        expected = session.get("_csrf_token", "")
        if not expected or not supplied or not hmac.compare_digest(str(supplied), str(expected)):
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"ok": False, "error": "Token di sicurezza non valido. Ricarica la pagina."}), 400
            abort(400)
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if not LOGIN_ENABLED:
        return redirect(url_for("index"))
    if session.get("authenticated"):
        return redirect(url_for("index"))

    raw_next = request.form.get("next") if request.method == "POST" else request.args.get("next")
    next_path = _safe_local_next(raw_next)
    error = None
    if request.method == "POST":
        supplied_csrf = request.form.get("csrf_token", "")
        expected_csrf = session.get("_csrf_token", "")
        if not expected_csrf or not supplied_csrf or not hmac.compare_digest(supplied_csrf, expected_csrf):
            error = "Sessione di accesso scaduta. Ricarica la pagina e riprova."
        else:
            ip = request.remote_addr or "unknown"
            wait = _login_wait_seconds(ip)
            if wait:
                error = f"Troppi tentativi. Riprova tra {max(1, wait)} secondi."
            else:
                password = request.form.get("password", "")
                if hmac.compare_digest(password.encode("utf-8"), APP_PASSWORD.encode("utf-8")):
                    with _login_attempts_lock:
                        _login_attempts.pop(ip, None)
                    session.clear()
                    session["authenticated"] = True
                    session.permanent = True
                    session["_csrf_token"] = secrets.token_urlsafe(32)
                    return redirect(next_path)
                _record_login_failure(ip)
                error = "Password non corretta."
    response = app.make_response(render_template("login.html", error=error, next_path=next_path))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login") if LOGIN_ENABLED else url_for("index"))

# ─── Helpers ──────────────────────────────────────────────────────────────────

_filename_lock = threading.Lock()
_reserved_recording_paths: set[str] = set()


def _generate_filename(channel_name: str) -> str:
    """Riserva un nome univoco per ogni sessione, anche con cambi qualità ravvicinati."""
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    desired = FILENAME_FORMAT.format(name=channel_name, timestamp=ts, ext=FILE_EXTENSION)
    with _filename_lock:
        candidate = desired
        stem, ext = os.path.splitext(desired)
        suffix = 2
        while candidate in _reserved_recording_paths or os.path.exists(os.path.join(RECORDINGS_DIR, candidate)):
            candidate = f"{stem}_{suffix}{ext}"
            suffix += 1
        _reserved_recording_paths.add(candidate)
        return candidate


def _as_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


_TIMESTAMP_FIELDS = (
    "last_live", "last_online", "last_checked", "last_status_change",
    "single_live_offline_since",
)


def _coerce_timestamp(value):
    """Converte timestamp Unix o ISO-8601 in secondi Unix; i valori senza fuso sono trattati come UTC."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            raw = str(value).strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return None


def _iso_timestamp(value):
    """Serializza un timestamp in ISO-8601 UTC con suffisso Z, ad esempio 2026-10-28T20:15:00Z."""
    timestamp = _coerce_timestamp(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def _load_channels() -> list[dict]:
    if os.path.exists(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if not isinstance(data, list):
                    logger.error("Il file %s deve contenere una lista JSON", CHANNELS_FILE)
                    return []
                for ch in data:
                    if not isinstance(ch, dict) or not ch.get("name"):
                        continue
                    if "quality" not in ch:
                        ch["quality"] = STREAM_QUALITY
                    ch["platform"] = _norm_platform(ch.get("platform"))
                    ch["sleeping"] = _as_bool(ch.get("sleeping", False))
                    if ch["sleeping"]:
                        ch["is_recording"] = False
                    ch["single_live_mode"] = _as_bool(ch.get("single_live_mode", False))
                    ch["single_live_seen_online"] = _as_bool(ch.get("single_live_seen_online", False))
                    ch["single_live_offline_since"] = _coerce_timestamp(ch.get("single_live_offline_since"))
                    if ch["sleeping"]:
                        ch["single_live_mode"] = False
                        ch["single_live_seen_online"] = False
                        ch["single_live_offline_since"] = None
                    ch["last_online"] = _coerce_timestamp(ch.get("last_online"))
                    ch["last_live"] = _coerce_timestamp(ch.get("last_live") or ch.get("last_online"))
                    ch["last_checked"] = _coerce_timestamp(ch.get("last_checked"))
                    ch["last_status_change"] = _coerce_timestamp(ch.get("last_status_change"))
                    if ch.get("last_status") not in ("online", "offline", "private"):
                        ch["last_status"] = "unknown"
                    if ch.get("last_check_result") not in (
                        "online", "offline", "private", "verifying_offline", "error", "checking"
                    ):
                        ch["last_check_result"] = "checking"
                    try:
                        ch["check_failures"] = max(0, int(ch.get("check_failures", 0)))
                    except (TypeError, ValueError):
                        ch["check_failures"] = 0
                    ch["last_error"] = str(ch.get("last_error", ""))[:300]
                    ch.pop("online", None)  # lo stato corrente viene ricalcolato dal monitor
                return [ch for ch in data if isinstance(ch, dict) and ch.get("name")]
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Impossibile leggere %s: %s", CHANNELS_FILE, exc)
    return []


def _save_channels(data: list[dict]) -> None:
    """Salva timestamp leggibili ISO-8601 UTC senza alterare i valori numerici usati a runtime."""
    tmp = CHANNELS_FILE + ".tmp"
    try:
        serializable = []
        for channel in data:
            item = dict(channel)
            for field in _TIMESTAMP_FIELDS:
                if field in item:
                    item[field] = _iso_timestamp(item.get(field))
            # Campo storico mantenuto per compatibilità con installazioni e client precedenti.
            if item.get("last_live") is None and item.get("last_online") is not None:
                item["last_live"] = _iso_timestamp(item["last_online"])
            serializable.append(item)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(serializable, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, CHANNELS_FILE)
    except OSError as exc:
        logger.error("Impossibile salvare %s: %s", CHANNELS_FILE, exc)


def _clear_single_live_state(channel: dict) -> None:
    """Disattiva la modalità one-live e azzera il relativo timer persistente."""
    channel["single_live_mode"] = False
    channel["single_live_seen_online"] = False
    channel["single_live_offline_since"] = None


def _update_single_live_mode(channel: dict, is_online: bool, now: float | None = None) -> bool:
    """Aggiorna il timer one-live su uno stato online/offline certo. Ritorna True se è scattata la pausa."""
    if not channel.get("single_live_mode"):
        return False
    now = time.time() if now is None else now

    if is_online:
        channel["single_live_seen_online"] = True
        if channel.get("single_live_offline_since") is not None:
            channel["single_live_offline_since"] = None
            logger.info("▶ %s è tornato online durante l'attesa Una volta; timer azzerato", channel.get("name", "canale"))
        return False

    # Non avviare il timer finché la modalità non ha visto almeno una live.
    if not channel.get("single_live_seen_online"):
        return False

    offline_since = channel.get("single_live_offline_since")
    if offline_since is None:
        channel["single_live_offline_since"] = now
        logger.info("⏳ %s offline: attendo fino a 2 ore una nuova live prima della pausa automatica", channel.get("name", "canale"))
        return False

    if now - float(offline_since) >= SINGLE_LIVE_GRACE_SECONDS:
        channel["is_recording"] = False
        _clear_single_live_state(channel)
        logger.info("⏸ %s messo in pausa automaticamente: nessuna nuova live entro 2 ore", channel.get("name", "canale"))
        return True
    return False


def _get_twitch_app_token() -> str:
    """Acquisisce e memorizza un app access token per Twitch Helix."""
    global _twitch_app_token, _twitch_app_token_expires
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET):
        raise RuntimeError("mancano TWITCH_CLIENT_ID e/o TWITCH_CLIENT_SECRET")
    now = time.time()
    with _twitch_token_lock:
        if _twitch_app_token and _twitch_app_token_expires > now + 30:
            return _twitch_app_token
        payload = urllib.parse.urlencode({
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://id.twitch.tv/oauth2/token", data=payload, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            token_data = json.loads(response.read().decode("utf-8"))
        token = str(token_data.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("Twitch non ha restituito un app access token valido")
        expires_in = max(60, int(token_data.get("expires_in", 3600)))
        _twitch_app_token = token
        _twitch_app_token_expires = time.time() + expires_in - 30
        return token


def _get_twitch_live_info(channel_name: str) -> tuple[str, dict | None, str | None]:
    """Ritorna (live|offline|error, metadati, errore) usando Twitch Helix e cache breve."""
    login = (channel_name or "").strip().lower()
    if not login:
        return "error", None, "nome canale Twitch vuoto"
    if not (TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET):
        return "error", None, "SKIP_RERUNS richiede TWITCH_CLIENT_ID e TWITCH_CLIENT_SECRET"
    now = time.time()
    with _twitch_meta_lock:
        cached = _twitch_meta_cache.get(login)
        if cached and cached[0] > now:
            return cached[1], cached[2], cached[3]

    state, stream, error = "error", None, None
    try:
        token = _get_twitch_app_token()
        query = urllib.parse.urlencode({"user_login": login})
        req = urllib.request.Request(
            f"https://api.twitch.tv/helix/streams?{query}",
            headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
        rows = body.get("data")
        if not isinstance(rows, list):
            raise RuntimeError("risposta Twitch Helix senza campo data valido")
        match = next((row for row in rows if isinstance(row, dict)
                      and str(row.get("user_login", "")).lower() == login), None)
        if match is not None:
            state, stream = "live", match
        else:
            state = "offline"
    except Exception as exc:
        error = f"Verifica rerun Twitch non disponibile: {type(exc).__name__}: {exc}"[:300]
        logger.warning("⚠ %s — %s", login, error)

    ttl = _twitch_meta_cache_ttl if state != "error" else 8.0
    with _twitch_meta_lock:
        _twitch_meta_cache[login] = (time.time() + ttl, state, stream, error)
    return state, stream, error


def _is_twitch_rerun_metadata(stream: dict | None) -> bool:
    """Rileva il tag Twitch ufficiale Rerun e un prefisso esplicito nel titolo."""
    if not isinstance(stream, dict):
        return False
    tags = stream.get("tags") or []
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict):
                tag = tag.get("name") or tag.get("tag_name") or ""
            normalized = re.sub(r"[^a-z0-9]", "", str(tag).casefold())
            if normalized == "rerun":
                return True
    title = str(stream.get("title") or "")
    return bool(re.match(r"^\s*(?:(?:\[|\()\s*)?re[\s-]?run\b", title, re.IGNORECASE))


def _streamlink_base_command(platform: str) -> list[str]:
    """Costruisce la base di Streamlink senza passare opzioni Twitch rimosse nelle versioni recenti."""
    global _streamlink_legacy_ads_supported
    cmd = ["streamlink"]
    if _norm_platform(platform) == "twitch" and SKIP_TWITCH_ADS:
        with _streamlink_legacy_ads_lock:
            if _streamlink_legacy_ads_supported is None:
                try:
                    probe = subprocess.run(
                        ["streamlink", "--twitch-disable-ads", "--version"],
                        capture_output=True, text=True, timeout=8,
                    )
                    _streamlink_legacy_ads_supported = probe.returncode == 0
                except Exception:
                    _streamlink_legacy_ads_supported = False
            supported = _streamlink_legacy_ads_supported
        if supported:
            cmd.append("--twitch-disable-ads")
    return cmd


def _resolve_quality(channel_name: str, preferred: str, platform: str = DEFAULT_PLATFORM) -> str:
    try:
        result = subprocess.run(
            ["streamlink", "--json", _platform_url(platform, channel_name)],
            capture_output=True, timeout=15,
        )
        if result.returncode != 0:
            return preferred

        info = json.loads(result.stdout)
        available = list(info.get("streams", {}).keys())

        if not available:
            return preferred
        if preferred in available:
            return preferred

        try:
            start_idx = QUALITY_OPTIONS.index(preferred)
        except ValueError:
            start_idx = 0

        for q in QUALITY_OPTIONS[start_idx:]:
            if q in available:
                logger.info("%s: %s non disponibile, uso %s", channel_name, preferred, q)
                return q
        for q in reversed(QUALITY_OPTIONS[:start_idx]):
            if q in available:
                logger.info("%s: %s non disponibile, uso %s (superiore)", channel_name, preferred, q)
                return q
        return "best"
    except Exception:
        return preferred


def _is_channel_online(channel_name: str, platform: str = DEFAULT_PLATFORM) -> tuple[bool, bool, str | None]:
    """Ritorna (online, risposta_certa, errore). Le errori transitori non diventano offline."""
    platform = _norm_platform(platform)
    try:
        result = subprocess.run(
            ["streamlink", "--json", _platform_url(platform, channel_name)],
            capture_output=True, text=True, timeout=15,
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        combined = f"{stdout} {stderr}".strip()
        parsed = None
        if stdout:
            try:
                parsed = json.loads(stdout)
            except json.JSONDecodeError:
                parsed = None

        lowered = combined.lower()
        private_phrases = (
            "this stream is private", "this channel is private", "private stream",
            "stream is private", "subscriber-only", "subscribers-only",
            "subscriber only", "subscribers only", "sub-only stream",
            "only available to subscribers",
        )
        if any(phrase in lowered for phrase in private_phrases):
            return False, True, "__private_stream__"

        streamlink_online: bool | None = None
        if isinstance(parsed, dict) and isinstance(parsed.get("streams"), dict):
            streamlink_online = bool(parsed.get("streams"))
        elif isinstance(parsed, dict) and isinstance(parsed.get("streams"), list):
            streamlink_online = bool(parsed.get("streams"))

        # Twitch non espone il tag Rerun nel JSON locale di Streamlink. Con SKIP_RERUNS
        # lo verifichiamo tramite Helix, che include i tag ufficiali della trasmissione.
        if platform == "twitch" and SKIP_RERUNS:
            api_state, stream_info, api_error = _get_twitch_live_info(channel_name)
            if api_state == "live":
                if _is_twitch_rerun_metadata(stream_info):
                    return False, True, "__rerun__"
                if streamlink_online is True:
                    return True, True, None
                return False, False, (
                    "Twitch segnala una live non-Rerun, ma Streamlink non ha trovato un flusso riproducibile"
                )
            if api_state == "error" and streamlink_online is True:
                return False, False, api_error or "Verifica rerun Twitch non disponibile"
            if api_state == "offline":
                if streamlink_online is True:
                    return False, False, "Streamlink trova un flusso, ma Twitch Helix non conferma una live"
                return False, True, None
            # Se Helix fallisce e Streamlink non vede una sorgente, la classificazione
            # offline resta sicura ai fini REC: non c'è un flusso che possa essere registrato.

        # Il JSON dei flussi è il segnale più affidabile, inclusa la lista vuota.
        if streamlink_online is not None:
            return streamlink_online, True, None

        offline_phrases = (
            "no playable streams found", "no streams found", "is offline",
            "currently offline", "not live", "stream is offline", "no streams available",
        )
        if any(phrase in lowered for phrase in offline_phrases):
            return False, True, None

        if result.returncode == 0:
            return False, False, (combined or "Risposta Streamlink senza stato riconoscibile")[:300]
        return False, False, (combined or f"Streamlink ha restituito codice {result.returncode}")[:300]
    except subprocess.TimeoutExpired:
        logger.warning("⚠ %s — timeout controllo online", channel_name)
        return False, False, "Timeout durante il controllo della diretta"
    except Exception as exc:
        logger.warning("⚠ %s — errore controllo online: %s", channel_name, exc)
        return False, False, str(exc)[:300]


def _file_info(path: str) -> dict:
    try:
        stat = os.stat(path)
        name = os.path.basename(path)
        # I nomi generati dal registratore contengono l'ora di inizio della parte.
        # Usarla per l'ordinamento impedisce ai file REC di saltare in lista quando
        # la loro data di modifica cambia mentre FFmpeg scrive nuovi pacchetti.
        match = re.search(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})", name)
        if match:
            try:
                order_ts = datetime.strptime(match.group(1), "%Y-%m-%d_%H-%M-%S").timestamp()
            except ValueError:
                order_ts = stat.st_mtime
        else:
            order_ts = stat.st_mtime
        return {
            "name": name,
            "size": stat.st_size,
            "size_human": _human_size(stat.st_size),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "modified_ts": stat.st_mtime,
            "order_ts": order_ts,
        }
    except OSError:
        return {"name": os.path.basename(path), "size": 0,
                "size_human": "—", "modified": "—", "modified_ts": 0, "order_ts": 0}


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    elif s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def _active_file_order_timestamps() -> dict[str, float]:
    """Ordine stabile per file attualmente in scrittura, anche con nomi personalizzati."""
    active: dict[str, float] = {}
    try:
        recorders = list(_recorders.values())
    except RuntimeError:
        recorders = []
    for rec in recorders:
        if rec.is_recording and rec.output_path:
            active[os.path.abspath(rec.output_path)] = float(rec.started_at or time.time())
    return active


def _active_file_paths() -> set[str]:
    return set(_active_file_order_timestamps())


def _list_recordings() -> list[dict]:
    files = []
    active_order = _active_file_order_timestamps()
    active = set(active_order)
    try:
        for name in os.listdir(RECORDINGS_DIR):
            ext = os.path.splitext(name)[1].lower()
            if ext in _VIDEO_EXTS:
                full = os.path.join(RECORDINGS_DIR, name)
                info = _file_info(full)
                path_key = os.path.abspath(full)
                info["in_use"] = path_key in active
                # Se FILENAME_FORMAT non include il timestamp standard, usa un
                # riferimento del recorder che non cambia a ogni scrittura.
                if path_key in active_order and not re.search(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", name):
                    info["order_ts"] = active_order[path_key]
                files.append(info)
    except OSError:
        pass
    files.sort(key=lambda f: (f.get("order_ts", f["modified_ts"]), f["name"].casefold()), reverse=True)
    return files


def _disk_stats() -> dict:
    total, used, free = shutil.disk_usage(RECORDINGS_DIR)
    return {
        "total": total, "used": used, "free": free,
        "total_human": _human_size(total),
        "used_human": _human_size(used),
        "free_human": _human_size(free),
        "used_pct": round(used / total * 100, 1) if total else 0,
    }


# ─── Elaborazione facoltativa delle registrazioni concluse ────────────────────

_dedup_lock = threading.Lock()
_convert_pool = threading.Semaphore(2)


def _load_dedup_hashes() -> dict[str, list[str]]:
    try:
        with open(DEDUP_FILE, "r", encoding="utf-8") as fh:
            value = json.load(fh)
        if not isinstance(value, dict):
            return {}
        return {
            str(channel): [str(item) for item in values if isinstance(item, str)]
            for channel, values in value.items() if isinstance(values, list)
        }
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_dedup_hashes(data: dict[str, list[str]]) -> None:
    temp_path = DEDUP_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(DEDUP_FILE)), exist_ok=True)
        with open(temp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(temp_path, DEDUP_FILE)
    except OSError as exc:
        logger.error("Impossibile salvare l'indice deduplicazione: %s", exc)
        try:
            os.remove(temp_path)
        except OSError:
            pass


def _compute_file_hash(filepath: str, nbytes: int = DEDUP_BYTES) -> str | None:
    """Fingerprint prudente: dimensione più campioni distribuiti; file brevi letti integralmente."""
    try:
        sample_size = max(64 * 1024, int(nbytes))
        with open(filepath, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            if size <= 0:
                return None
            digest = hashlib.sha256()
            digest.update(f"size:{size};sample:{sample_size};".encode("ascii"))
            if size <= 2 * sample_size:
                digest.update(b"full:")
                fh.seek(0)
                digest.update(fh.read())
            else:
                digest.update(b"head:")
                fh.seek(0)
                digest.update(fh.read(sample_size))
                digest.update(b";middle:")
                fh.seek(max(0, size // 2 - sample_size // 2))
                digest.update(fh.read(sample_size))
                digest.update(b";tail:")
                fh.seek(-sample_size, os.SEEK_END)
                digest.update(fh.read(sample_size))
            return digest.hexdigest()
    except (OSError, ValueError, TypeError):
        return None


def _is_duplicate(channel_name: str, filepath: str) -> bool:
    if not DEDUP_ENABLED:
        return False
    fingerprint = _compute_file_hash(filepath)
    if not fingerprint:
        return False
    with _dedup_lock:
        hashes = _load_dedup_hashes()
        channel_hashes = hashes.get(channel_name, [])
        if fingerprint in channel_hashes:
            logger.info("Duplicato rilevato per %s: %s", channel_name, os.path.basename(filepath))
            return True
        channel_hashes.append(fingerprint)
        hashes[channel_name] = channel_hashes[-50:]
        _save_dedup_hashes(hashes)
    return False


def _auto_convert(filepath: str) -> None:
    if not AUTO_CONVERT or os.path.splitext(filepath)[1].lower() != ".ts":
        return
    try:
        if not os.path.isfile(filepath) or os.path.getsize(filepath) < 4096:
            return
    except OSError:
        return
    threading.Thread(
        target=_do_convert, args=(filepath,), daemon=True,
        name=f"convert-{os.path.basename(filepath)[:40]}",
    ).start()


def _do_convert(ts_path: str) -> None:
    """Remuxa un TS concluso in MP4 senza ricodifica; il sorgente è conservato per default."""
    stem, ext = os.path.splitext(ts_path)
    if ext.lower() != ".ts":
        return
    mp4_path = stem + ".mp4"
    if os.path.exists(mp4_path):
        logger.info("MP4 già presente, conversione saltata: %s", os.path.basename(mp4_path))
        return
    with _convert_pool:
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-n",
                 "-i", ts_path, "-map", "0", "-c", "copy", "-movflags", "+faststart", mp4_path],
                capture_output=True, timeout=3600,
            )
            if result.returncode == 0 and os.path.isfile(mp4_path) and os.path.getsize(mp4_path) > 1024:
                logger.info("Conversione completata: %s", os.path.basename(mp4_path))
                if DELETE_AFTER_CONVERT:
                    try:
                        os.remove(ts_path)
                    except OSError as exc:
                        logger.warning("MP4 creato ma TS non eliminato (%s): %s", os.path.basename(ts_path), exc)
            else:
                details = (result.stderr or b"").decode("utf-8", errors="replace")[-300:]
                logger.error("Conversione MP4 fallita per %s: %s", os.path.basename(ts_path), details)
                try:
                    if os.path.exists(mp4_path):
                        os.remove(mp4_path)
                except OSError:
                    pass
        except subprocess.TimeoutExpired:
            logger.error("Timeout durante la conversione di %s", os.path.basename(ts_path))
            try:
                if os.path.exists(mp4_path):
                    os.remove(mp4_path)
            except OSError:
                pass
        except Exception as exc:
            logger.error("Errore conversione %s: %s", os.path.basename(ts_path), exc)
        finally:
            _sse_broadcast("update")


def _finalize_recording_file(channel_name: str, filepath: str | None) -> None:
    """Processa solo un segmento già chiuso: la conversione/dedup non tocca file aperti."""
    if not filepath:
        return
    try:
        if not os.path.isfile(filepath) or os.path.getsize(filepath) < 4096:
            return
    except OSError:
        return
    if _is_duplicate(channel_name, filepath):
        try:
            os.remove(filepath)
            logger.warning("Registrazione duplicata eliminata: %s", os.path.basename(filepath))
        except OSError as exc:
            logger.error("Non riesco a eliminare il duplicato %s: %s", os.path.basename(filepath), exc)
        _sse_broadcast("update")
        return
    _auto_convert(filepath)
    _sse_broadcast("update")


# ─── Notifiche ────────────────────────────────────────────────────────────────

def _send_notification(event: str, channel: str, message: str) -> None:
    if event not in NOTIFY_EVENTS or not NOTIFY_TYPE:
        return
    threading.Thread(target=_do_notify, args=(event, channel, message), daemon=True).start()


def _do_notify(event: str, channel: str, message: str) -> None:
    try:
        if NOTIFY_TYPE == "discord" and NOTIFY_URL:
            _notify_discord(channel, message)
        elif NOTIFY_TYPE == "telegram" and TELEGRAM_TOKEN and TELEGRAM_CHAT:
            _notify_telegram(event, channel, message)
        elif NOTIFY_TYPE == "webhook" and NOTIFY_URL:
            _notify_webhook(event, channel, message)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        logger.error("❌ Notifica %s/%s — HTTP %d: %s | Body: %s",
                     NOTIFY_TYPE, channel, exc.code, exc.reason, body)
    except urllib.error.URLError as exc:
        logger.error("❌ Notifica %s/%s — Errore connessione: %s", NOTIFY_TYPE, channel, exc.reason)
    except Exception as exc:
        logger.error("❌ Notifica %s/%s — Errore: %s", NOTIFY_TYPE, channel, exc)


def _notify_discord(channel: str, message: str) -> None:
    payload = json.dumps({
        "username": "Twitch-Recorder",
        "embeds": [{
            "title": f"\U0001f4f9 {channel}",
            "description": message,
            "color": 0x9146FF,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "footer": {"text": "Twitch-Recorder"},
        }],
    }).encode()
    req = urllib.request.Request(NOTIFY_URL, data=payload,
                                headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10)


def _tg_escape_html(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _notify_telegram(event: str, channel: str, message: str) -> None:
    platform = _platform_of(channel)
    channel_url = _platform_url(platform, channel)
    base_api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    safe_ch = _tg_escape_html(channel)
    link = f'<a href="{channel_url}">{safe_ch}</a>'

    if event == "online":
        caption = f"{link} — 🟢 Online"

        # La thumbnail pubblica è disponibile solo per Twitch
        if TELEGRAM_PHOTO and platform == "twitch":
            photo_url = f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{channel}-640x360.jpg"
            payload = json.dumps({
                "chat_id": TELEGRAM_CHAT,
                "photo": photo_url,
                "caption": caption,
                "parse_mode": "HTML",
            }).encode()
            try:
                req = urllib.request.Request(f"{base_api}/sendPhoto", data=payload,
                                            headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=15)
                return
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")[:300]
                except Exception:
                    pass
                logger.warning("⚠ Telegram sendPhoto fallito per %s — HTTP %d: %s | %s — fallback a testo",
                               channel, exc.code, exc.reason, body)
            except Exception as exc:
                logger.warning("⚠ Telegram sendPhoto fallito per %s — %s — fallback a testo", channel, exc)

        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode()
        url = f"{base_api}/sendMessage"

    elif event == "offline":
        text = f"{link} — 🔴 Offline"
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode()
        url = f"{base_api}/sendMessage"

    else:
        text = f"{link}\n{_tg_escape_html(message)}"
        payload = json.dumps({
            "chat_id": TELEGRAM_CHAT,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode()
        url = f"{base_api}/sendMessage"

    req = urllib.request.Request(url, data=payload,
                                headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=15)


def _notify_webhook(event: str, channel: str, message: str) -> None:
    payload = json.dumps({
        "event": event, "channel": channel, "message": message,
        "timestamp": datetime.utcnow().isoformat() + "Z", "source": "twitch-recorder",
    }).encode()
    req = urllib.request.Request(NOTIFY_URL, data=payload,
                                headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10)


class LiveStreamHub:
    """Buffer circolare in memoria per condividere lo stream REC con la preview."""

    def __init__(self, max_bytes: int = LIVE_STREAM_BUFFER_BYTES):
        self.max_bytes = max(512 * 1024, int(max_bytes))
        self._condition = threading.Condition()
        self._chunks = deque()
        self._bytes = 0
        self._sequence = 0
        self._closed = False
        self.started_at = time.time()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def publish(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._condition:
            if self._closed:
                return
            self._sequence += 1
            payload = bytes(chunk)
            self._chunks.append((self._sequence, payload))
            self._bytes += len(payload)
            while self._bytes > self.max_bytes and len(self._chunks) > 1:
                _sequence, old = self._chunks.popleft()
                self._bytes -= len(old)
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def iter_chunks(self):
        """Invia il buffer recente e segue i dati live senza bloccare il recorder."""
        with self._condition:
            cursor = self._chunks[0][0] - 1 if self._chunks else self._sequence
        while True:
            with self._condition:
                while True:
                    oldest = self._chunks[0][0] if self._chunks else self._sequence + 1
                    if cursor < oldest - 1:
                        cursor = oldest - 1
                    next_chunk = next(((seq, data) for seq, data in self._chunks if seq > cursor), None)
                    if next_chunk is not None:
                        cursor, payload = next_chunk
                        break
                    if self._closed:
                        return
                    self._condition.wait(timeout=15)
            yield payload


# ─── Recorder ────────────────────────────────────────────────────────────────

class Recorder:
    __slots__ = (
        "channel_name", "process", "source_process", "output_path",
        "is_recording", "stop_requested", "_lock", "_thread", "_log_fh",
        "started_at", "quality", "_split_triggered", "platform",
        "shared_stream", "reconnect_attempts", "last_error",
    )

    def __init__(self, channel_name: str, quality: str = "best", platform: str = DEFAULT_PLATFORM):
        self.channel_name = channel_name
        self.quality = quality
        self.platform = _norm_platform(platform)
        # process = FFmpeg remuxer che alimenta sia il file sia la preview;
        # source_process = unica connessione Streamlink alla piattaforma.
        self.process: subprocess.Popen | None = None
        self.source_process: subprocess.Popen | None = None
        self.output_path: str | None = None
        self.is_recording = False
        self.stop_requested = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._log_fh = None
        self.started_at: float | None = None
        self._split_triggered = False
        self.shared_stream: LiveStreamHub | None = None
        self.reconnect_attempts = 0
        self.last_error = ""

    def start(self) -> None:
        with self._lock:
            if self.is_recording:
                return
            self.stop_requested = False
            self.is_recording = True
            self.started_at = time.time()
            self.last_error = ""
            self._thread = threading.Thread(
                target=self._manager_loop, daemon=True,
                name=f"rec-{self.channel_name}",
            )
            self._thread.start()
            logger.info("▶ Avviata registrazione: %s @ %s", self.channel_name, self.quality)
        _send_notification("rec_start", self.channel_name, f"Registrazione avviata a {self.quality}")
        _sse_broadcast("update")

    def stop(self) -> None:
        with self._lock:
            if not self.is_recording:
                return
            self.stop_requested = True
            proc = self.process
            source_proc = self.source_process
            hub = self.shared_stream
        # Fermare prima il remux chiude anche la pipe d'ingresso; terminiamo poi
        # esplicitamente Streamlink per evitare che resti una connessione sospesa.
        for child in (proc, source_proc):
            if child and child.poll() is None:
                try:
                    child.terminate()
                except OSError:
                    pass
        if hub:
            # I dati già buffered restano leggibili; il generator termina appena li ha inviati.
            hub.close()
        logger.info("⏹ Stop: %s", self.channel_name)

    @property
    def uptime(self) -> float:
        if self.started_at and self.is_recording:
            return time.time() - self.started_at
        return 0.0

    def _manager_loop(self) -> None:
        error_start: float | None = None

        while not self.stop_requested:
            resolved = _resolve_quality(self.channel_name, self.quality, self.platform)
            self._split_triggered = False
            output_path = os.path.join(RECORDINGS_DIR, _generate_filename(self.channel_name))
            self.output_path = output_path
            hub = LiveStreamHub()
            with self._lock:
                self.shared_stream = hub

            streamlink_cmd = _streamlink_base_command(self.platform)
            streamlink_cmd += ["--stdout", _platform_url(self.platform, self.channel_name), resolved]
            # FFmpeg rimuxa il flusso senza ricodificare. Oltre a rendere più
            # affidabile la preview collegata a REC, normalizza i timestamp TS
            # così la durata del file parte dall'inizio della registrazione,
            # non dal tempo trascorso dall'avvio della live sulla piattaforma.
            ffmpeg_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
                "-fflags", "+genpts", "-i", "pipe:0",
                "-map", "0:v?", "-map", "0:a?", "-c", "copy",
                "-avoid_negative_ts", "make_zero", "-mpegts_copyts", "0",
                "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "-muxpreload", "0",
                "-flush_packets", "1", "-f", "mpegts", "pipe:1",
            ]
            log_path = os.path.join(RECORDINGS_DIR, f".{self.channel_name}.log")
            t0 = time.time()
            source_process = None
            process = None
            exit_code = None

            try:
                self._log_fh = open(log_path, "a", encoding="utf-8")
                # Un solo flusso sorgente: Streamlink -> FFmpeg remux -> file + hub.
                # Il byte stream normalizzato è identico per registrazione e preview.
                with open(output_path, "wb", buffering=0) as output_fh:
                    source_process = subprocess.Popen(
                        streamlink_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=self._log_fh, bufsize=0, start_new_session=True,
                    )
                    if source_process.stdout is None:
                        raise RuntimeError("Streamlink non ha aperto il flusso di uscita")
                    process = subprocess.Popen(
                        ffmpeg_cmd, stdin=source_process.stdout, stdout=subprocess.PIPE,
                        stderr=self._log_fh, bufsize=0, start_new_session=True,
                    )
                    # Il parent non deve tenere aperta la pipe ereditata da FFmpeg.
                    source_process.stdout.close()
                    with self._lock:
                        self.source_process = source_process
                        self.process = process
                    # Se stop viene premuto durante l'avvio dei processi, chiudili
                    # subito; il lettore continuerà comunque a drenare i byte finali.
                    if self.stop_requested:
                        for child in (process, source_process):
                            if child.poll() is None:
                                try:
                                    child.terminate()
                                except OSError:
                                    pass
                    threading.Thread(
                        target=self._watch_size,
                        args=(process, output_path),
                        daemon=True,
                        name=f"size-{self.channel_name}",
                    ).start()

                    stdout = process.stdout
                    if stdout is None:
                        raise RuntimeError("FFmpeg non ha aperto il flusso di uscita")
                    # Continua a leggere fino a EOF anche dopo Stop: FFmpeg gestisce
                    # SIGTERM e scarica gli ultimi pacchetti, così il .ts non viene
                    # troncato solo perché l'utente ha premuto pausa/stop.
                    while True:
                        chunk = stdout.read(64 * 1024)
                        if not chunk:
                            break
                        output_fh.write(chunk)
                        hub.publish(chunk)

                    if process.poll() is None and (self.stop_requested or self._split_triggered):
                        try:
                            process.terminate()
                        except OSError:
                            pass
                    try:
                        exit_code = process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        exit_code = process.wait(timeout=3)
                    try:
                        stdout.close()
                    except (OSError, ValueError):
                        pass
            except Exception as exc:
                self.last_error = f"Errore acquisizione/remux: {exc}"[:300]
                logger.error("Errore acquisizione/remux %s: %s", self.channel_name, exc)
            finally:
                # Chiude il remuxer e la sorgente in entrambe le direzioni, anche
                # se il player o il processo Streamlink si interrompono in modo anomalo.
                for child in (process, source_process):
                    if child is not None and child.poll() is None:
                        try:
                            child.terminate()
                            child.wait(timeout=3)
                        except (OSError, subprocess.TimeoutExpired):
                            try:
                                child.kill()
                                child.wait(timeout=2)
                            except (OSError, subprocess.TimeoutExpired):
                                pass
                if process is not None and process.stdout is not None:
                    try:
                        process.stdout.close()
                    except (OSError, ValueError):
                        pass
                if source_process is not None and source_process.stdout is not None:
                    try:
                        source_process.stdout.close()
                    except (OSError, ValueError):
                        pass
                hub.close()
                with self._lock:
                    if self.shared_stream is hub:
                        self.shared_stream = None
                    if self.process is process:
                        self.process = None
                    if self.source_process is source_process:
                        self.source_process = None
                    if self.output_path == output_path:
                        # Questo segmento è chiuso. Il recorder può riaprire un nuovo file
                        # al prossimo tentativo, ma il file completato non va più marcato REC.
                        self.output_path = None
                if self._log_fh:
                    try:
                        self._log_fh.close()
                    except OSError:
                        pass
                    self._log_fh = None
                _finalize_recording_file(self.channel_name, output_path)

            if self.stop_requested:
                break

            elapsed = time.time() - t0
            if self._split_triggered:
                logger.info("✂ %s: split dopo %s — riavvio", self.channel_name, _fmt_duration(elapsed))
                time.sleep(1)
                continue

            if elapsed < 20:
                if not self.last_error:
                    self.last_error = f"Flusso interrotto rapidamente (codice FFmpeg {exit_code})"
                if error_start is None:
                    error_start = time.time()
                if time.time() - error_start > 180:
                    self.last_error = "Tolleranza errori superata dopo tentativi di riconnessione"
                    logger.error("Tolleranza errori superata per %s — stop.", self.channel_name)
                    break
                if self._smart_retry():
                    error_start = None
                    continue
                # Se il controllo è incerto, non viene dichiarato offline qui: il monitor
                # conserva lo stato confermato e decide con le proprie verifiche adattive.
                break
            else:
                error_start = None
                self.reconnect_attempts = 0
                self.last_error = ""
                logger.info("Fine segmento %s dopo %s — riavvio.", self.channel_name, _fmt_duration(elapsed))
                time.sleep(1)

        with self._lock:
            hub = self.shared_stream
            self.shared_stream = None
            self.is_recording = False
            self.started_at = None
            process = self.process
            source_process = self.source_process
            self.process = None
            self.source_process = None
        if hub:
            hub.close()
        for child in (process, source_process):
            if child and child.poll() is None:
                try:
                    child.terminate()
                except OSError:
                    pass
        _send_notification("rec_end", self.channel_name, "Registrazione terminata")
        _sse_broadcast("update")

    def _smart_retry(self) -> bool:
        for attempt in range(RECONNECT_TRIES):
            if self.stop_requested:
                return False
            wait = RECONNECT_WAIT + random.uniform(0, 10)
            self.reconnect_attempts = attempt + 1
            logger.info("🔁 %s: riconnessione %d/%d — attendo %.0fs…",
                        self.channel_name, attempt + 1, RECONNECT_TRIES, wait)
            time.sleep(wait)
            if self.stop_requested:
                return False
            is_online, is_certain, err = _is_channel_online(self.channel_name, self.platform)
            if is_online and is_certain:
                logger.info("✅ %s: stream ancora attivo, riprendo registrazione", self.channel_name)
                self.last_error = ""
                return True
            if not is_certain and err:
                self.last_error = f"Stato stream incerto: {err}"[:300]
        logger.info("❌ %s: stream non recuperato dopo %d tentativi", self.channel_name, RECONNECT_TRIES)
        if not self.last_error:
            self.last_error = f"Stream non recuperato dopo {RECONNECT_TRIES} tentativi"
        return False

    def _watch_size(self, proc: subprocess.Popen, path: str) -> None:
        if MAX_FILE_SIZE <= 0:
            return
        threshold = max(int(MAX_FILE_SIZE * 0.97), MAX_FILE_SIZE - 20 * 1024 * 1024)
        while not self.stop_requested:
            if proc.poll() is not None:
                return
            try:
                if os.path.exists(path) and os.path.getsize(path) >= threshold:
                    logger.info("Split %s (%s)", path, _human_size(os.path.getsize(path)))
                    self._split_triggered = True
                    proc.terminate()
                    return
            except OSError:
                pass
            time.sleep(2)


# ─── Stato globale ────────────────────────────────────────────────────────────

_lock = threading.Lock()
_channels: list[dict] = _load_channels()
# Migra al primo avvio i vecchi timestamp Unix in formato ISO-8601 UTC.
if _channels:
    _save_channels(_channels)
_recorders: dict[str, Recorder] = {
    ch["name"]: Recorder(ch["name"], ch.get("quality", STREAM_QUALITY), ch.get("platform", DEFAULT_PLATFORM))
    for ch in _channels
}
_quality_change_locks: dict[str, threading.Lock] = {}
_online_pool = ThreadPoolExecutor(max_workers=MONITOR_WORKERS, thread_name_prefix="online-check")
_online_status: dict[str, bool] = {
    # Conserva l'ultimo stato confermato durante il riavvio; il monitor esegue
    # comunque un nuovo controllo appena il canale è eleggibile.
    ch["name"]: ch.get("last_status") == "online"
    for ch in _channels
}
_last_checked: dict[str, float] = {
    ch["name"]: float(ch.get("last_checked") or 0) for ch in _channels
}
_next_check_due: dict[str, float] = {ch["name"]: 0.0 for ch in _channels}
_offline_candidates: dict[str, int] = {}
_offline_stop_due: dict[str, float] = {}
_force_check: set[str] = set()
_monitor_wake = threading.Event()
_monitor_inflight: dict[str, float] = {}
_monitor_heartbeat: float = 0.0
_monitor_last_progress: float = 0.0

# Processi di preview temporanei; vengono chiusi appena il client interrompe il player.
_preview_processes_lock = threading.Lock()
_preview_processes: set[tuple[subprocess.Popen | None, subprocess.Popen | None]] = set()
_preview_state_lock = threading.Lock()
_preview_states: dict[str, dict[str, int]] = {}
_sse_subscribers: list[queue.Queue] = []
_sse_lock = threading.Lock()


def _preview_state_key(platform: str, room: str) -> str:
    return f"{_norm_platform(platform)}:{room}"


def _preview_started(platform: str, room: str, mode: str) -> None:
    key = _preview_state_key(platform, room)
    with _preview_state_lock:
        state = _preview_states.setdefault(key, {"shared": 0, "temporary": 0})
        state[mode] = state.get(mode, 0) + 1
    _sse_broadcast("update")


def _preview_ended(platform: str, room: str, mode: str) -> None:
    key = _preview_state_key(platform, room)
    with _preview_state_lock:
        state = _preview_states.get(key)
        if state:
            state[mode] = max(0, state.get(mode, 0) - 1)
            if not state.get("shared") and not state.get("temporary"):
                _preview_states.pop(key, None)
    _sse_broadcast("update")


def _read_preview_state(platform: str, room: str) -> dict:
    key = _preview_state_key(platform, room)
    with _preview_state_lock:
        state = dict(_preview_states.get(key, {"shared": 0, "temporary": 0}))
    if state.get("shared", 0):
        mode = "shared"
    elif state.get("temporary", 0):
        mode = "temporary"
    else:
        mode = "off"
    return {"active": bool(state.get("shared", 0) or state.get("temporary", 0)),
            "mode": mode, "connections": state.get("shared", 0) + state.get("temporary", 0)}


# ─── SSE ──────────────────────────────────────────────────────────────────────

def _sse_broadcast(event: str = "status") -> None:
    data = _build_status_json()
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_subscribers:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                _sse_subscribers.remove(q)
            except ValueError:
                pass


def _build_status_json() -> dict:
    now = time.time()
    with _lock:
        ch_list = []
        for ch in _channels:
            name = ch["name"]
            rec = _recorders.get(name)
            lc = _last_checked.get(name, 0)
            plat = _norm_platform(ch.get("platform"))
            preview = _read_preview_state(plat, name)
            if rec:
                with rec._lock:
                    rec_is_recording = bool(rec.is_recording)
                    rec_uptime = rec.uptime
                    rec_stream = rec.shared_stream
                    reconnect_attempts = rec.reconnect_attempts
                    recorder_error = rec.last_error
            else:
                rec_is_recording, rec_uptime, rec_stream = False, 0, None
                reconnect_attempts, recorder_error = 0, ""
            ch_list.append({
                "name": name,
                "platform": plat,
                "platform_label": PLATFORMS[plat]["label"],
                "platform_short": PLATFORMS[plat]["short"],
                "platform_color": PLATFORMS[plat]["color"],
                "home_url": _platform_url(plat, name),
                "online": bool(_online_status.get(name, False) and not ch.get("sleeping", False)),
                "sleeping": bool(ch.get("sleeping", False)),
                "last_status": ch.get("last_status", "unknown"),
                "last_check_result": ch.get("last_check_result", "checking"),
                "check_failures": int(ch.get("check_failures", 0)),
                "offline_confirmation_count": _offline_candidates.get(name, 0),
                "offline_confirmations_required": OFFLINE_CONFIRMATIONS,
                "last_error": ch.get("last_error") or recorder_error,
                "is_recording": ch.get("is_recording", False),
                "actually_recording": rec_is_recording,
                "uptime": round(rec_uptime) if rec else 0,
                "uptime_human": _fmt_duration(rec_uptime) if rec and rec_uptime > 0 else None,
                "reconnect_attempts": reconnect_attempts,
                "quality": ch.get("quality", STREAM_QUALITY),
                "single_live_mode": bool(ch.get("single_live_mode", False)),
                "single_live_seen_online": bool(ch.get("single_live_seen_online", False)),
                "single_live_offline_remaining": (
                    max(0, int(SINGLE_LIVE_GRACE_SECONDS - (now - ch["single_live_offline_since"])))
                    if ch.get("single_live_mode") and ch.get("single_live_offline_since") is not None else None
                ),
                "current_file": os.path.basename(rec.output_path) if rec and rec.output_path else None,
                "last_checked": round(now - lc) if lc > 0 else None,
                # L'API di stato mantiene i timestamp Unix numerici per compatibilità;
                # channels.json ed export serializzano gli stessi campi in ISO-8601 UTC.
                "last_live": ch.get("last_live") or ch.get("last_online"),
                "last_online": ch.get("last_online"),
                "last_status_change": ch.get("last_status_change"),
                "preview_active": preview["active"],
                "preview_mode": preview["mode"],
                "preview_connections": preview["connections"],
                "shared_stream_available": bool(rec_stream and not rec_stream.closed),
            })
        inflight = dict(_monitor_inflight)
        last_progress = _monitor_last_progress
    recordings = _list_recordings()
    disk = _disk_stats()
    total_rec_size = sum(r["size"] for r in recordings)
    monitor_alive = bool("_monitor_thread" in globals() and _monitor_thread.is_alive())
    oldest_check_age = max((now - started for started in inflight.values()), default=0)
    heartbeat_age = now - _monitor_heartbeat if _monitor_heartbeat > 0 else None
    monitor_state = "stopped" if not monitor_alive else (
        "degraded" if oldest_check_age > 45 or (heartbeat_age is not None and heartbeat_age > 45) else "ok"
    )
    return {
        "channels": ch_list,
        "recordings": recordings,
        "disk": disk,
        "quality_options": QUALITY_OPTIONS,
        "platforms": [{"key": k, "label": v["label"], "short": v["short"], "color": v["color"]}
                      for k, v in PLATFORMS.items()],
        "config": {
            "reconnect_wait": RECONNECT_WAIT,
            "reconnect_tries": RECONNECT_TRIES,
            "notify_type": NOTIFY_TYPE or "none",
            "monitor_workers": MONITOR_WORKERS,
            "online_check_interval": ONLINE_CHECK_INTERVAL,
            "offline_check_interval": OFFLINE_CHECK_INTERVAL,
            "error_check_interval": ERROR_CHECK_INTERVAL,
            "offline_confirmations": OFFLINE_CONFIRMATIONS,
            "offline_confirm_interval": OFFLINE_CONFIRM_INTERVAL,
            "recorder_offline_grace_seconds": RECORDER_OFFLINE_GRACE_SECONDS,
            "auto_convert": AUTO_CONVERT,
            "delete_after_convert": DELETE_AFTER_CONVERT,
            "dedup_enabled": DEDUP_ENABLED,
            "skip_reruns": SKIP_RERUNS,
            "skip_twitch_ads": SKIP_TWITCH_ADS,
        },
        "stats": {
            "total_channels": len(ch_list),
            "online_channels": sum(1 for c in ch_list if c["online"]),
            "active_recordings": sum(1 for c in ch_list if c["actually_recording"]),
            "active_previews": sum(1 for c in ch_list if c["preview_active"]),
            "total_files": len(recordings),
            "total_size": total_rec_size,
            "total_size_human": _human_size(total_rec_size),
            "monitor_alive": monitor_alive,
            "monitor_state": monitor_state,
            "monitor_last_cycle": round(now - last_progress) if last_progress > 0 else None,
            "monitor_heartbeat": round(heartbeat_age) if heartbeat_age is not None else None,
            "monitor_inflight": len(inflight),
            "monitor_inflight_oldest": round(oldest_check_age) if inflight else 0,
            "monitor_workers": MONITOR_WORKERS,
        },
    }


# ─── Monitor adattivo ─────────────────────────────────────────────────────────

def _apply_check_result(name: str, result: tuple[bool, bool, str | None]) -> None:
    """Aggiorna stato persistente, applica isteresi offline e pianifica il prossimo check."""
    global _monitor_last_progress
    is_online, is_certain, err = result
    now = time.time()
    to_start = None
    to_stop = None
    notification = None

    with _lock:
        ch = next((item for item in _channels if item["name"] == name), None)
        if ch is None:
            _monitor_inflight.pop(name, None)
            return
        if ch.get("sleeping", False):
            _monitor_inflight.pop(name, None)
            _offline_candidates.pop(name, None)
            _offline_stop_due.pop(name, None)
            _monitor_last_progress = now
            # La UI riceve un refresh fuori dal lock globale.
            threading.Timer(0.05, _sse_broadcast, args=("update",)).start()
            return
        rec = _recorders.get(name)
        old_status = ch.get("last_status", "unknown")
        was_online = bool(_online_status.get(name, False) or old_status == "online")
        ch["last_checked"] = now
        _last_checked[name] = now

        if not is_certain:
            ch["check_failures"] = int(ch.get("check_failures", 0)) + 1
            ch["last_error"] = (err or "Errore temporaneo durante il controllo")[:300]
            ch["last_check_result"] = "error"
            _offline_candidates.pop(name, None)
            effective_online = bool(_online_status.get(name, False) or old_status == "online")
            _online_status[name] = effective_online
            interval = ERROR_CHECK_INTERVAL
            # Durante la finestra di tolleranza già avviata per REC si continua a
            # ricontrollare rapidamente; gli altri errori ricevono backoff adattivo.
            if name in _offline_stop_due:
                interval = min(interval, OFFLINE_CONFIRM_INTERVAL)
        elif is_online:
            _offline_candidates.pop(name, None)
            _online_status[name] = True
            ch["last_status"] = "online"
            ch["last_check_result"] = "online"
            ch["last_error"] = ""
            ch["check_failures"] = 0
            ch["last_live"] = now
            ch["last_online"] = now  # alias compatibile con le versioni precedenti
            if old_status != "online":
                ch["last_status_change"] = now
            if not was_online:
                notification = ("online", name, "Il canale è online!")
            _offline_stop_due.pop(name, None)
            _update_single_live_mode(ch, True, now)
            interval = ONLINE_CHECK_INTERVAL
            if rec:
                rec.quality = ch.get("quality", STREAM_QUALITY)
                if ch.get("is_recording", False) and not rec.is_recording:
                    to_start = rec
        elif is_certain and err == "__rerun__":
            # Una rerun non conta come una nuova live e non aggiorna last_live.
            _offline_candidates.pop(name, None)
            _offline_stop_due.pop(name, None)
            _online_status[name] = False
            ch["last_status"] = "rerun"
            ch["last_check_result"] = "rerun"
            ch["last_error"] = ""
            ch["check_failures"] = 0
            if old_status != "rerun":
                ch["last_status_change"] = now
                logger.info("⏭ %s: rerun Twitch rilevata, registrazione saltata", name)
            _update_single_live_mode(ch, False, now)
            if rec and rec.is_recording:
                to_stop = rec
            interval = OFFLINE_CHECK_INTERVAL
        elif is_certain and err == "__private_stream__":
            # Stato privato distinto da offline: non aggiornare last_live e non avviare il timer Una volta.
            _offline_candidates.pop(name, None)
            _offline_stop_due.pop(name, None)
            _online_status[name] = False
            ch["last_status"] = "private"
            ch["last_check_result"] = "private"
            ch["last_error"] = ""
            ch["check_failures"] = 0
            if old_status != "private":
                ch["last_status_change"] = now
            if rec and rec.is_recording:
                to_stop = rec
            interval = ONLINE_CHECK_INTERVAL
        else:
            # Un singolo esito negativo non ferma la registrazione: si conferma
            # solo se il canale era noto online, una registrazione è ancora attiva
            # o Una volta aveva già rilevato una live non ancora conclusa.
            had_confirmed_live = bool(
                was_online or old_status == "online"
                or (ch.get("single_live_seen_online") and ch.get("single_live_offline_since") is None)
            )
            needs_offline_confirmation = bool(
                old_status != "offline" and (had_confirmed_live or (rec and rec.is_recording))
            )
            if needs_offline_confirmation:
                count = _offline_candidates.get(name, 0) + 1
                _offline_candidates[name] = count
                if count < OFFLINE_CONFIRMATIONS:
                    # Il valore online resta confermato fino all'esito finale.
                    _online_status[name] = True
                    ch["last_check_result"] = "verifying_offline"
                    ch["last_error"] = ""
                    ch["check_failures"] = 0
                    interval = OFFLINE_CONFIRM_INTERVAL
                else:
                    _offline_candidates.pop(name, None)
                    _online_status[name] = False
                    ch["last_status"] = "offline"
                    ch["last_check_result"] = "offline"
                    ch["last_error"] = ""
                    ch["check_failures"] = 0
                    if old_status != "offline":
                        ch["last_status_change"] = now
                    if had_confirmed_live:
                        notification = ("offline", name, "Il canale è offline (confermato)")
                    paused_by_once = _update_single_live_mode(ch, False, now)
                    if rec:
                        if paused_by_once or not ch.get("is_recording", False):
                            _offline_stop_due.pop(name, None)
                            if rec.is_recording:
                                to_stop = rec
                            interval = OFFLINE_CHECK_INTERVAL
                        elif rec.is_recording:
                            due = _offline_stop_due.get(name)
                            if due is None:
                                due = now + RECORDER_OFFLINE_GRACE_SECONDS
                                _offline_stop_due[name] = due
                                logger.info("⏳ %s: offline confermato; attendo %ds prima di fermare REC",
                                            name, RECORDER_OFFLINE_GRACE_SECONDS)
                            if now < due:
                                interval = min(OFFLINE_CONFIRM_INTERVAL, max(1, int(due - now)))
                            else:
                                _offline_stop_due.pop(name, None)
                                to_stop = rec
                                interval = OFFLINE_CHECK_INTERVAL
                        else:
                            interval = OFFLINE_CHECK_INTERVAL
                    else:
                        interval = OFFLINE_CHECK_INTERVAL
            else:
                # Stato offline già confermato: aggiorna il timer Una volta e gestisce
                # l'eventuale finestra di tolleranza senza ripartire da nuove conferme.
                _offline_candidates.pop(name, None)
                _online_status[name] = False
                ch["last_status"] = "offline"
                ch["last_check_result"] = "offline"
                ch["last_error"] = ""
                ch["check_failures"] = 0
                if old_status not in ("offline", "unknown"):
                    ch["last_status_change"] = now
                paused_by_once = _update_single_live_mode(ch, False, now)
                if rec:
                    if paused_by_once or not ch.get("is_recording", False):
                        _offline_stop_due.pop(name, None)
                        if rec.is_recording:
                            to_stop = rec
                        interval = OFFLINE_CHECK_INTERVAL
                    elif rec.is_recording:
                        due = _offline_stop_due.get(name)
                        if due is None:
                            due = now + RECORDER_OFFLINE_GRACE_SECONDS
                            _offline_stop_due[name] = due
                        if now < due:
                            interval = min(OFFLINE_CONFIRM_INTERVAL, max(1, int(due - now)))
                        else:
                            _offline_stop_due.pop(name, None)
                            to_stop = rec
                            interval = OFFLINE_CHECK_INTERVAL
                    else:
                        _offline_stop_due.pop(name, None)
                        interval = OFFLINE_CHECK_INTERVAL
                else:
                    interval = OFFLINE_CHECK_INTERVAL

        # Durante l'attesa Una volta il monitor accelera gradualmente vicino alla scadenza.
        if ch.get("single_live_mode") and ch.get("single_live_offline_since") is not None:
            remaining = SINGLE_LIVE_GRACE_SECONDS - (now - float(ch["single_live_offline_since"]))
            if remaining <= 120:
                interval = min(interval, OFFLINE_CONFIRM_INTERVAL)
            else:
                interval = min(interval, max(1, int(remaining)))

        _next_check_due[name] = now + max(1, interval)
        _monitor_inflight.pop(name, None)
        _save_channels(_channels)

    if notification:
        threading.Thread(target=_send_notification, args=notification, daemon=True,
                         name=f"notify-{notification[0]}-{name}").start()
    if to_stop:
        to_stop.stop()
    if to_start:
        with _lock:
            current = next((item for item in _channels if item["name"] == name), None)
            may_start = bool(current and current.get("is_recording", False) and not current.get("sleeping", False))
        if may_start:
            to_start.start()
            with _lock:
                current = next((item for item in _channels if item["name"] == name), None)
                still_allowed = bool(current and current.get("is_recording", False) and not current.get("sleeping", False))
            if not still_allowed:
                to_start.stop()
            else:
                logger.info("▶ Avvio registrazione %s dopo controllo online confermato", name)
    _monitor_last_progress = time.time()
    _sse_broadcast("update")


def _monitor_loop() -> None:
    global _monitor_heartbeat, _monitor_last_progress
    inflight: dict[Future, tuple[str, float]] = {}
    logger.info(
        "🚀 Monitor adattivo avviato — worker=%d, online=%ds, offline=%ds, errori=%ds, conferme offline=%d",
        MONITOR_WORKERS, ONLINE_CHECK_INTERVAL, OFFLINE_CHECK_INTERVAL,
        ERROR_CHECK_INTERVAL, OFFLINE_CONFIRMATIONS,
    )

    while True:
        cycle_now = time.time()
        try:
            # Applica prima i risultati pronti, senza attendere i controlli più lenti.
            for future, (name, started_at) in list(inflight.items()):
                if not future.done():
                    continue
                inflight.pop(future, None)
                try:
                    result = future.result()
                except Exception as exc:
                    result = (False, False, str(exc)[:300])
                _apply_check_result(name, result)

            with _lock:
                snapshot = copy.deepcopy(_channels)
                forced = set(_force_check)
                for name in forced:
                    _next_check_due[name] = 0.0
                in_flight_names = {name for name, _started in inflight.values()}
                eligible = []
                for ch in snapshot:
                    name = ch["name"]
                    if ch.get("sleeping", False):
                        # La modalità sonno sospende tutti i controlli automatici.
                        _force_check.discard(name)
                        continue
                    # Una pausa normale ferma soltanto la registrazione: il monitor
                    # continua a rilevare le nuove live per ogni canale non dormiente.
                    if name in in_flight_names:
                        continue
                    due_at = _next_check_due.get(name, 0.0)
                    if name in forced or due_at <= cycle_now:
                        rec = _recorders.get(name)
                        has_active_rec = bool(rec and rec.is_recording)
                        eligible.append((name, ch.get("platform", DEFAULT_PLATFORM), due_at,
                                         name in forced, has_active_rec, bool(ch.get("single_live_mode"))))

                # Check richiesti dall'utente in testa; poi quelli più in ritardo. La
                # quantità di processi in volo non supera MONITOR_WORKERS.
                eligible.sort(key=lambda item: (
                    0 if item[3] else 1,
                    0 if item[4] else 1,
                    0 if item[5] else 1,
                    item[2], item[0].lower(),
                ))
                free_slots = max(0, MONITOR_WORKERS - len(inflight))
                submitted_names = set()
                for name, platform, _due, _forced, _active_rec, _once in eligible[:free_slots]:
                    future = _online_pool.submit(_is_channel_online, name, _norm_platform(platform))
                    inflight[future] = (name, cycle_now)
                    _monitor_inflight[name] = cycle_now
                    submitted_names.add(name)
                    # Non viene rimesso in coda finché questa richiesta non termina.
                    _next_check_due[name] = cycle_now + ERROR_CHECK_INTERVAL
                _force_check.difference_update(submitted_names)

                _monitor_heartbeat = time.time()

            if eligible and not inflight:
                # Caso raro: i canali sono stati rimossi fra lo snapshot e la submit.
                _monitor_wake.wait(0.2)
            else:
                _monitor_wake.wait(0.5)
            _monitor_wake.clear()

        except Exception as exc:
            logger.error("Errore nel ciclo monitor: %s", exc, exc_info=True)
            _monitor_wake.wait(1)


_monitor_thread = threading.Thread(target=_monitor_loop, daemon=True, name="monitor")
_monitor_thread.start()


# ─── Watchdog ─────────────────────────────────────────────────────────────────

def _watchdog_loop() -> None:
    global _monitor_thread
    while True:
        time.sleep(30)
        try:
            if not _monitor_thread.is_alive():
                logger.error("Monitor terminato — riavvio del thread")
                _monitor_thread = threading.Thread(target=_monitor_loop, daemon=True, name="monitor")
                _monitor_thread.start()
                _monitor_wake.set()
            else:
                with _lock:
                    stale_checks = [time.time() - started for started in _monitor_inflight.values()]
                if stale_checks and max(stale_checks) > 45:
                    logger.warning("Monitor degradato: controllo senza risposta da %.0fs", max(stale_checks))
        except Exception as exc:
            logger.error("Errore watchdog: %s", exc)


threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog").start()


# ─── SSE broadcast dopo azioni POST ──────────────────────────────────────────

@app.after_request
def _after_request_sse(response):
    if request.method == "POST" and request.endpoint in (
        "index", "delete_recording", "api_import", "api_force_check"
    ):
        threading.Timer(0.3, _sse_broadcast, args=("action",)).start()
    return response


# ─── Rotte pagina ────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def index():
    global _channels

    if request.method == "POST":
        action = request.form.get("action", "")
        platform = _norm_platform(request.form.get("platform"))
        channel_name = _clean_name(platform, request.form.get("channel") or "")
        quality = request.form.get("quality", STREAM_QUALITY)

        if action == "add" and channel_name:
            with _lock:
                if any(c["name"] == channel_name for c in _channels):
                    flash("Canale già presente.", "warning")
                else:
                    entry = {"name": channel_name, "is_recording": True, "sleeping": False,
                             "quality": quality, "platform": platform}
                    _channels.append(entry)
                    _recorders[channel_name] = Recorder(channel_name, quality, platform)
                    _save_channels(_channels)
                    _force_check.add(channel_name)
                    _next_check_due[channel_name] = 0.0
                    flash(f"{channel_name} aggiunto.", "success")
            _monitor_wake.set()

        elif action in ("pause", "resume") and channel_name:
            with _lock:
                ch = next((c for c in _channels if c["name"] == channel_name), None)
                if ch:
                    rec = _recorders.get(channel_name)
                    _clear_single_live_state(ch)
                    if action == "pause":
                        ch["is_recording"] = False
                        if rec and rec.is_recording:
                            rec.stop()
                    else:
                        if not ch.get("sleeping", False):
                            ch["is_recording"] = True
                    _save_channels(_channels)
                    _next_check_due[channel_name] = 0.0
                    if not ch.get("sleeping", False):
                        _force_check.add(channel_name)
            _monitor_wake.set()

        elif action == "remove" and channel_name:
            with _lock:
                rec = _recorders.pop(channel_name, None)
                if rec:
                    rec.stop()
                _channels = [c for c in _channels if c["name"] != channel_name]
                _online_status.pop(channel_name, None)
                _last_checked.pop(channel_name, None)
                _next_check_due.pop(channel_name, None)
                _offline_candidates.pop(channel_name, None)
                _offline_stop_due.pop(channel_name, None)
                _force_check.discard(channel_name)
                _save_channels(_channels)
            _monitor_wake.set()

        elif action == "set_quality" and channel_name:
            with _lock:
                ch = next((c for c in _channels if c["name"] == channel_name), None)
                if ch:
                    ch["quality"] = quality
                    rec = _recorders.get(channel_name)
                    if rec:
                        rec.quality = quality
                    _save_channels(_channels)
            _monitor_wake.set()

        elif action == "pause_all":
            with _lock:
                for ch in _channels:
                    _clear_single_live_state(ch)
                    ch["is_recording"] = False
                    rec = _recorders.get(ch["name"])
                    if rec and rec.is_recording:
                        rec.stop()
                _save_channels(_channels)

        elif action == "resume_all":
            with _lock:
                for ch in _channels:
                    if ch.get("sleeping", False):
                        continue
                    _clear_single_live_state(ch)
                    ch["is_recording"] = True
                    _force_check.add(ch["name"])
                    _next_check_due[ch["name"]] = 0.0
                _save_channels(_channels)

        return redirect(url_for("index"))

    return render_template("index.html")


@app.route("/recordings/<path:filename>")
def download_recording(filename):
    safe = os.path.basename(filename)
    parts = Path(filename).parts
    if len(parts) > 2 or ".." in parts:
        abort(400)
    return send_from_directory(RECORDINGS_DIR, safe)


def _live_tail_offset(source, delay_seconds=8):
    """Find a recent PAT before the target PCR; preserve original A/V timestamps."""
    packet_size = 188
    source.seek(0, os.SEEK_END)
    size = source.tell()
    start = max(0, size - 16 * 1024 * 1024)
    start -= start % packet_size
    source.seek(start)
    data = source.read(size - start)
    clocks = []
    last_pat = None
    last_key_pat = None
    for offset in range(0, len(data) - packet_size + 1, packet_size):
        packet = data[offset:offset + packet_size]
        if packet[0] != 0x47:
            continue
        pid = ((packet[1] & 0x1f) << 8) | packet[2]
        if pid == 0 and packet[1] & 0x40:
            last_pat = start + offset
        if packet[3] & 0x20 and packet[4] >= 7 and packet[5] & 0x10:
            if packet[5] & 0x40:
                last_key_pat = last_pat
            p = packet[6:12]
            pcr = (p[0] << 25) | (p[1] << 17) | (p[2] << 9) | (p[3] << 1) | (p[4] >> 7)
            if last_pat is not None:
                clocks.append((pcr, last_key_pat if last_key_pat is not None else last_pat))
    if not clocks:
        return 0 if start == 0 else (last_pat or start)
    latest = clocks[-1][0]
    # PCR wraps every 2**33 ticks. Pick the closest point at least 8s behind.
    for clock, pat in reversed(clocks):
        if (latest - clock) % (1 << 33) >= delay_seconds * 90000:
            return pat
    return clocks[0][1]


def _stop_process_group(proc: subprocess.Popen | None, label: str) -> None:
    """Termina un processo temporaneo e l'eventuale gruppo di processi figli."""
    if proc is None:
        return
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                logger.warning("Preview: impossibile attendere la chiusura di %s", label)
        except OSError:
            pass
    for stream_name in ("stdout", "stderr", "stdin"):
        stream = getattr(proc, stream_name, None)
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _stop_preview_processes(processes: tuple[subprocess.Popen | None, subprocess.Popen | None]) -> None:
    """Chiude FFmpeg e Streamlink; la funzione è sicura anche se chiamata due volte."""
    with _preview_processes_lock:
        _preview_processes.discard(processes)
    streamlink_proc, ffmpeg_proc = processes
    # Chiudere prima il consumatore, poi la sorgente che gli alimenta stdin.
    _stop_process_group(ffmpeg_proc, "ffmpeg")
    _stop_process_group(streamlink_proc, "streamlink")


@app.route("/live/<platform>/<room>")
def preview_live(platform, room):
    """Serve la preview dal flusso REC condiviso oppure da una sorgente temporanea."""
    platform = _norm_platform(platform)
    with _lock:
        channel = next((c for c in _channels if c["name"] == room
                        and _norm_platform(c.get("platform")) == platform), None)
        if channel is None:
            abort(404)
        quality = channel.get("quality", STREAM_QUALITY)
        rec = _recorders.get(room)

    # REC attiva: il subscriber legge lo stesso flusso già acquisito dal recorder.
    # Un FFmpeg leggero per il solo player ricrea un MPEG-TS con header/timestamp
    # validi per una connessione iniziata a live già avviata; non apre una nuova
    # connessione alla piattaforma e non scrive alcun file.
    shared_hub = None
    if rec is not None:
        with rec._lock:
            if rec.is_recording:
                shared_hub = rec.shared_stream
    if shared_hub is not None and not shared_hub.closed:
        preview_ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-probesize", "1048576", "-analyzeduration", "1000000",
            "-fflags", "+genpts", "-i", "pipe:0",
            "-map", "0:v?", "-map", "0:a?", "-c", "copy",
            "-avoid_negative_ts", "make_zero", "-mpegts_copyts", "0",
            "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "-muxpreload", "0",
            "-flush_packets", "1", "-f", "mpegts", "pipe:1",
        ]
        ffmpeg_proc = None
        try:
            ffmpeg_proc = subprocess.Popen(
                preview_ffmpeg_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True,
            )
            if ffmpeg_proc.stdin is None or ffmpeg_proc.stdout is None:
                raise RuntimeError("FFmpeg non ha aperto le pipe della preview condivisa")
            processes = (None, ffmpeg_proc)
            with _preview_processes_lock:
                _preview_processes.add(processes)
        except Exception as exc:
            logger.warning("Impossibile avviare il remux della preview condivisa %s: %s", room, exc)
            _stop_preview_processes((None, ffmpeg_proc))
            return jsonify(error="Impossibile avviare la preview durante la registrazione."), 503

        _preview_started(platform, room, "shared")
        lifecycle_lock = threading.Lock()
        lifecycle = {"ended": False}

        def finish_shared():
            with lifecycle_lock:
                if lifecycle["ended"]:
                    return
                lifecycle["ended"] = True
            _stop_preview_processes(processes)
            _preview_ended(platform, room, "shared")

        def feed_shared():
            """Invia al remux per-client i byte della stessa acquisizione REC."""
            stdin = ffmpeg_proc.stdin
            try:
                if stdin is None:
                    return
                for payload in shared_hub.iter_chunks():
                    if ffmpeg_proc.poll() is not None:
                        break
                    try:
                        view = memoryview(payload)
                        while view:
                            written = stdin.write(view)
                            if not written:
                                raise BrokenPipeError("pipe preview chiusa")
                            view = view[written:]
                    except (BrokenPipeError, OSError, ValueError):
                        break
            except (GeneratorExit, BrokenPipeError, OSError, ValueError):
                pass
            finally:
                if stdin is not None:
                    try:
                        stdin.close()
                    except (BrokenPipeError, OSError, ValueError):
                        pass

        threading.Thread(
            target=feed_shared, daemon=True, name=f"preview-shared-{room}"
        ).start()

        def shared_generate():
            try:
                stdout = ffmpeg_proc.stdout
                if stdout is None:
                    return
                while True:
                    try:
                        ready, _, _ = select.select([stdout], [], [], 30)
                    except (OSError, ValueError):
                        break
                    if not ready:
                        if ffmpeg_proc.poll() is not None:
                            break
                        logger.warning("Preview condivisa di %s senza dati da 30 secondi: chiusura", room)
                        break
                    try:
                        chunk = os.read(stdout.fileno(), 64 * 1024)
                    except OSError:
                        break
                    if not chunk:
                        break
                    yield chunk
            except (GeneratorExit, BrokenPipeError, ConnectionResetError):
                pass
            finally:
                finish_shared()

        response = Response(shared_generate(), mimetype="video/mp2t", headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
            "X-Preview-Source": "recorder-shared",
        })
        response.call_on_close(finish_shared)
        return response

    # REC ferma: la preview crea un flusso temporaneo. Nessun file viene scritto e
    # il player chiuso interrompe sia FFmpeg sia Streamlink.
    resolved_quality = _resolve_quality(room, quality, platform)
    streamlink_cmd = _streamlink_base_command(platform)
    streamlink_cmd += ["--stdout", _platform_url(platform, room), resolved_quality]

    streamlink_proc = None
    ffmpeg_proc = None
    try:
        streamlink_proc = subprocess.Popen(
            streamlink_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True,
        )
        if streamlink_proc.stdout is None:
            raise RuntimeError("Streamlink non ha aperto il flusso di uscita")

        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-fflags", "+genpts", "-i", "pipe:0",
            "-map", "0:v?", "-map", "0:a?", "-c", "copy",
            "-avoid_negative_ts", "make_zero", "-mpegts_copyts", "0",
            "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "-muxpreload", "0",
            "-flush_packets", "1", "-f", "mpegts", "pipe:1",
        ]
        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd, stdin=streamlink_proc.stdout, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True,
        )
        streamlink_proc.stdout.close()
        processes = (streamlink_proc, ffmpeg_proc)
        with _preview_processes_lock:
            _preview_processes.add(processes)
    except Exception as exc:
        logger.warning("Impossibile avviare la preview live di %s (%s): %s", room, platform, exc)
        _stop_preview_processes((streamlink_proc, ffmpeg_proc))
        return jsonify(error="Impossibile avviare il flusso live. Verifica che la diretta sia online."), 503

    _preview_started(platform, room, "temporary")
    lifecycle_lock = threading.Lock()
    lifecycle = {"ended": False}

    def finish_temporary():
        with lifecycle_lock:
            if lifecycle["ended"]:
                return
            lifecycle["ended"] = True
        _stop_preview_processes((streamlink_proc, ffmpeg_proc))
        _preview_ended(platform, room, "temporary")

    def generate():
        try:
            stdout = ffmpeg_proc.stdout
            if stdout is None:
                return
            while True:
                try:
                    ready, _, _ = select.select([stdout], [], [], 30)
                except (OSError, ValueError):
                    break
                if not ready:
                    if ffmpeg_proc.poll() is not None or streamlink_proc.poll() is not None:
                        break
                    logger.warning("Preview temporanea di %s senza dati da 30 secondi: chiusura", room)
                    break
                try:
                    chunk = os.read(stdout.fileno(), 64 * 1024)
                except OSError:
                    break
                if not chunk:
                    break
                yield chunk
        except (GeneratorExit, BrokenPipeError, ConnectionResetError):
            pass
        finally:
            finish_temporary()

    response = Response(generate(), mimetype="video/mp2t", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "X-Accel-Buffering": "no",
        "X-Content-Type-Options": "nosniff",
        "X-Preview-Source": "temporary",
    })
    response.call_on_close(finish_temporary)
    return response


@app.route("/delete_recording", methods=["POST"])
def delete_recording():
    filename = request.form.get("filename", "")
    safe = os.path.basename(filename)
    path = os.path.join(RECORDINGS_DIR, safe)
    if os.path.exists(path):
        if os.path.abspath(path) in _active_file_paths():
            flash("Impossibile eliminare: registrazione in corso.", "danger")
        else:
            try:
                os.remove(path)
            except OSError as exc:
                flash(f"Errore: {exc}", "danger")
    return redirect(url_for("index"))


# ─── API JSON ─────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    return jsonify(_build_status_json())


@app.route("/api/stream")
def api_stream():
    """SSE endpoint per aggiornamenti live in tempo reale."""
    def event_stream():
        q = queue.Queue(maxsize=50)
        with _sse_lock:
            _sse_subscribers.append(q)
        try:
            data = _build_status_json()
            yield f"event: status\ndata: {json.dumps(data)}\n\n"
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield msg
                    while not q.empty():
                        try:
                            yield q.get_nowait()
                        except queue.Empty:
                            break
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                try:
                    _sse_subscribers.remove(q)
                except ValueError:
                    pass

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/export")
def api_export():
    fmt = request.args.get("format", "json").lower()
    with _lock:
        data = [{"name": ch["name"], "platform": _norm_platform(ch.get("platform")),
                 "quality": ch.get("quality", STREAM_QUALITY),
                 "is_recording": ch.get("is_recording", False),
                 "sleeping": ch.get("sleeping", False),
                 "single_live_mode": bool(ch.get("single_live_mode", False)),
                 "single_live_seen_online": bool(ch.get("single_live_seen_online", False)),
                 "single_live_offline_since": _iso_timestamp(ch.get("single_live_offline_since")),
                 "last_live": _iso_timestamp(ch.get("last_live") or ch.get("last_online")),
                 "last_online": _iso_timestamp(ch.get("last_online")),
                 "last_status": ch.get("last_status", "unknown"),
                 "last_checked": _iso_timestamp(ch.get("last_checked")),
                 "last_status_change": _iso_timestamp(ch.get("last_status_change"))}
                for ch in _channels]

    if fmt == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["name", "platform", "quality", "is_recording", "sleeping",
                                                    "single_live_mode", "single_live_seen_online", "single_live_offline_since",
                                                    "last_live", "last_online", "last_status", "last_checked", "last_status_change"])
        writer.writeheader()
        writer.writerows(data)
        return Response(output.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=twitch-channels.csv"})

    return Response(json.dumps(data, indent=2), mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=twitch-channels.json"})


@app.route("/api/import", methods=["POST"])
def api_import():
    global _channels
    imported = 0
    skipped = 0

    try:
        file = request.files.get("file")
        if file:
            content = file.read().decode("utf-8")
        else:
            content = request.get_data(as_text=True)

        rooms: list[dict] = []
        content_stripped = content.strip()

        if content_stripped.startswith("[") or content_stripped.startswith("{"):
            parsed = json.loads(content_stripped)
            if isinstance(parsed, dict):
                parsed = [parsed]
            for item in parsed:
                if isinstance(item, str):
                    rooms.append({"name": item, "platform": DEFAULT_PLATFORM,
                                  "quality": STREAM_QUALITY, "is_recording": True})
                elif isinstance(item, dict) and "name" in item:
                    rooms.append({
                        "name": item["name"],
                        "platform": _norm_platform(item.get("platform")),
                        "quality": item.get("quality", STREAM_QUALITY),
                        "is_recording": _as_bool(item.get("is_recording", True), True),
                        "sleeping": _as_bool(item.get("sleeping", False)),
                        "single_live_mode": _as_bool(item.get("single_live_mode", False)),
                        "single_live_seen_online": _as_bool(item.get("single_live_seen_online", False)),
                        "single_live_offline_since": _coerce_timestamp(item.get("single_live_offline_since")),
                        "last_live": _coerce_timestamp(item.get("last_live") or item.get("last_online")),
                        "last_online": _coerce_timestamp(item.get("last_online") or item.get("last_live")),
                        "last_status": item.get("last_status", "unknown"),
                        "last_checked": _coerce_timestamp(item.get("last_checked")),
                        "last_status_change": _coerce_timestamp(item.get("last_status_change")),
                    })
        else:
            reader = csv.DictReader(io.StringIO(content))
            for row in reader:
                name = row.get("name", "").strip()
                if name:
                    rooms.append({
                        "name": name,
                        "platform": _norm_platform(row.get("platform")),
                        "quality": row.get("quality", STREAM_QUALITY),
                        "is_recording": _as_bool(row.get("is_recording", "true"), True),
                        "sleeping": _as_bool(row.get("sleeping", "false")),
                        "single_live_mode": _as_bool(row.get("single_live_mode", "false")),
                        "single_live_seen_online": _as_bool(row.get("single_live_seen_online", "false")),
                        "single_live_offline_since": _coerce_timestamp(row.get("single_live_offline_since")),
                        "last_live": _coerce_timestamp(row.get("last_live") or row.get("last_online")),
                        "last_online": _coerce_timestamp(row.get("last_online") or row.get("last_live")),
                        "last_status": row.get("last_status", "unknown"),
                        "last_checked": _coerce_timestamp(row.get("last_checked")),
                        "last_status_change": _coerce_timestamp(row.get("last_status_change")),
                    })

        with _lock:
            existing = {c["name"] for c in _channels}
            for room in rooms:
                plat = _norm_platform(room.get("platform"))
                name = _clean_name(plat, room["name"])
                if not name or name in existing:
                    skipped += 1
                    continue
                entry = {"name": name, "quality": room["quality"],
                         "is_recording": False if room.get("sleeping", False) else room["is_recording"],
                         "sleeping": room.get("sleeping", False), "platform": plat,
                         "single_live_mode": bool(room.get("single_live_mode", False)) and not room.get("sleeping", False),
                         "single_live_seen_online": bool(room.get("single_live_seen_online", False)) and not room.get("sleeping", False),
                         "single_live_offline_since": (
                             _coerce_timestamp(room.get("single_live_offline_since"))
                             if room.get("single_live_mode", False) and not room.get("sleeping", False) else None
                         ),
                         "last_live": _coerce_timestamp(room.get("last_live") or room.get("last_online")),
                         "last_online": _coerce_timestamp(room.get("last_online") or room.get("last_live")),
                         "last_status": room.get("last_status", "unknown"),
                         "last_checked": _coerce_timestamp(room.get("last_checked")),
                         "last_status_change": _coerce_timestamp(room.get("last_status_change")),
                         "last_check_result": "checking", "check_failures": 0, "last_error": ""}
                _channels.append(entry)
                _recorders[name] = Recorder(name, room["quality"], plat)
                if not entry["sleeping"]:
                    _force_check.add(name)
                _next_check_due[name] = 0.0
                existing.add(name)
                imported += 1
            _save_channels(_channels)
        _monitor_wake.set()

    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({"imported": imported, "skipped": skipped})


@app.route("/api/check", methods=["POST"])
def api_force_check():
    """Forza un check immediato per uno o tutti i canali."""
    room = ""
    if request.is_json:
        room = (request.json or {}).get("channel", "").strip()
    else:
        room = request.form.get("channel", "").strip()
    skipped_sleeping = False
    with _lock:
        if room:
            target = next((ch for ch in _channels if ch["name"] == room), None)
            if target and target.get("sleeping", False):
                skipped_sleeping = True
            elif target:
                _force_check.add(room)
        else:
            _force_check.update(ch["name"] for ch in _channels if not ch.get("sleeping", False))
        queued = len(_force_check)
    _monitor_wake.set()
    return jsonify({"queued": queued, "skipped_sleeping": skipped_sleeping})


@app.route("/api/room", methods=["POST"])
def api_room_action():
    """API JSON per tutte le azioni sui canali."""
    global _channels
    data = request.get_json(silent=True) or {}
    action = data.get("action", "")
    platform = _norm_platform(data.get("platform"))
    channel_name = _clean_name(platform, data.get("channel") or "")
    quality = data.get("quality", STREAM_QUALITY)

    if action == "add" and channel_name:
        with _lock:
            if any(c["name"] == channel_name for c in _channels):
                return jsonify({"ok": False, "msg": "Canale già presente (nome unico tra le piattaforme)."})
            entry = {"name": channel_name, "is_recording": True, "sleeping": False,
                     "quality": quality, "platform": platform}
            _channels.append(entry)
            _recorders[channel_name] = Recorder(channel_name, quality, platform)
            _save_channels(_channels)
            _force_check.add(channel_name)
            _next_check_due[channel_name] = 0.0
        _monitor_wake.set()
        _sse_broadcast("action")
        return jsonify({"ok": True, "msg": f"{channel_name} aggiunto ({PLATFORMS[platform]['label']})."})

    elif action == "toggle_single_live" and channel_name:
        with _lock:
            ch = next((c for c in _channels if c["name"] == channel_name), None)
            if not ch:
                return jsonify({"ok": False, "msg": "Canale non trovato."}), 404

            if ch.get("single_live_mode"):
                _clear_single_live_state(ch)
                msg = f"Modalità Una volta disattivata per {channel_name}."
            else:
                ch["sleeping"] = False
                ch["single_live_mode"] = True
                ch["single_live_seen_online"] = bool(_online_status.get(channel_name, False))
                ch["single_live_offline_since"] = None
                ch["is_recording"] = True
                _force_check.add(channel_name)
                _next_check_due[channel_name] = 0.0
                msg = (
                    f"Modalità Una volta attivata per {channel_name}: "
                    "dopo la fine della live attenderò 2 ore per una nuova diretta, poi metterò il canale in pausa."
                )
            _save_channels(_channels)
        _monitor_wake.set()
        _sse_broadcast("action")
        return jsonify({"ok": True, "msg": msg})

    elif action == "toggle_sleep" and channel_name:
        rec_to_stop = None
        with _lock:
            ch = next((c for c in _channels if c["name"] == channel_name), None)
            if not ch:
                return jsonify({"ok": False, "msg": "Canale non trovato."}), 404
            rec = _recorders.get(channel_name)
            if ch.get("sleeping", False):
                # Il risveglio riattiva solo il monitor: Play resta un'azione esplicita.
                ch["sleeping"] = False
                ch["is_recording"] = False
                _next_check_due[channel_name] = 0.0
                _force_check.add(channel_name)
                msg = f"{channel_name} risvegliato: monitor riattivato, registrazione ancora in pausa. Premi Play per registrare."
                logger.info("☀ %s risvegliato: monitor riattivato senza registrare", channel_name)
            else:
                ch["sleeping"] = True
                ch["is_recording"] = False
                _clear_single_live_state(ch)
                _next_check_due[channel_name] = float("inf")
                _force_check.discard(channel_name)
                _offline_candidates.pop(channel_name, None)
                _offline_stop_due.pop(channel_name, None)
                if rec:
                    # Stop è eseguito fuori dal lock globale; includiamo il recorder anche
                    # se sta completando il bootstrap in parallelo.
                    rec_to_stop = rec
                msg = f"{channel_name} messo a dormire: registrazione ferma e controlli automatici sospesi."
                logger.info("🌙 %s a dormire: monitor sospeso", channel_name)
            _save_channels(_channels)
        if rec_to_stop:
            rec_to_stop.stop()
        _monitor_wake.set()
        _sse_broadcast("action")
        return jsonify({"ok": True, "msg": msg, "sleeping": bool(ch.get("sleeping", False))})

    elif action in ("pause", "resume") and channel_name:
        rec_to_stop = None
        with _lock:
            ch = next((c for c in _channels if c["name"] == channel_name), None)
            if ch:
                rec = _recorders.get(channel_name)
                _clear_single_live_state(ch)
                _offline_stop_due.pop(channel_name, None)
                if action == "pause":
                    ch["is_recording"] = False
                    if rec and rec.is_recording:
                        rec_to_stop = rec
                else:
                    if not ch.get("sleeping", False):
                        ch["is_recording"] = True
                _save_channels(_channels)
                if not ch.get("sleeping", False):
                    _next_check_due[channel_name] = 0.0
                    _force_check.add(channel_name)
        if rec_to_stop:
            rec_to_stop.stop()
        _monitor_wake.set()
        _sse_broadcast("action")
        return jsonify({"ok": True})

    elif action == "remove" and channel_name:
        rec_to_stop = None
        with _lock:
            rec_to_stop = _recorders.pop(channel_name, None)
            _channels = [c for c in _channels if c["name"] != channel_name]
            _online_status.pop(channel_name, None)
            _last_checked.pop(channel_name, None)
            _next_check_due.pop(channel_name, None)
            _offline_candidates.pop(channel_name, None)
            _offline_stop_due.pop(channel_name, None)
            _force_check.discard(channel_name)
            _quality_change_locks.pop(channel_name, None)
            _save_channels(_channels)
        if rec_to_stop:
            rec_to_stop.stop()
        _monitor_wake.set()
        _sse_broadcast("action")
        return jsonify({"ok": True})

    elif action == "set_quality" and channel_name:
        if quality not in QUALITY_OPTIONS:
            return jsonify({"ok": False, "msg": "Qualità non valida."}), 400
        with _lock:
            quality_lock = _quality_change_locks.setdefault(channel_name, threading.Lock())

        # Serializza i cambi qualità dello stesso canale per non creare sessioni concorrenti.
        with quality_lock:
            with _lock:
                ch = next((c for c in _channels if c["name"] == channel_name), None)
                if not ch:
                    return jsonify({"ok": False, "msg": "Canale non trovato."}), 404
                previous_quality = ch.get("quality", STREAM_QUALITY)
                if previous_quality == quality:
                    return jsonify({"ok": True, "msg": "La qualità selezionata è già attiva.", "restarted": False})
                rec = _recorders.get(channel_name)
                was_recording = bool(rec and rec.is_recording)
                platform_for_channel = _norm_platform(ch.get("platform", platform))
                should_record = bool(ch.get("is_recording", False) and not ch.get("sleeping", False))
                ch["quality"] = quality
                if rec:
                    rec.quality = quality
                _save_channels(_channels)

            if was_recording and rec:
                # Finalizza il file precedente prima di installare il nuovo recorder.
                # Ogni sessione ottiene un nuovo nome, anche se il cambio avviene nello stesso secondo.
                rec.stop()
                old_thread = rec._thread
                if old_thread and old_thread is not threading.current_thread():
                    old_thread.join(timeout=15)
                if old_thread and old_thread.is_alive():
                    logger.warning("Cambio qualità %s: chiusura sessione ancora in corso; verrà completata in background", channel_name)
                    def _finish_quality_rollover(name, old_rec, selected_quality, selected_platform, serialization_lock):
                        with serialization_lock:
                            thread = old_rec._thread
                            if thread and thread is not threading.current_thread():
                                thread.join()
                            with _lock:
                                current = next((item for item in _channels if item["name"] == name), None)
                                if current is None or _recorders.get(name) is not old_rec:
                                    return
                                # Usa l'ultima scelta salvata: l'utente può aver cambiato la qualità
                                # di nuovo mentre il vecchio processo stava completando la chiusura.
                                final_quality = current.get("quality", selected_quality)
                                final_platform = _norm_platform(current.get("platform", selected_platform))
                                replacement = Recorder(name, final_quality, final_platform)
                                _recorders[name] = replacement
                                if current.get("is_recording", False) and not current.get("sleeping", False):
                                    _force_check.add(name)
                                    _next_check_due[name] = 0.0
                                _save_channels(_channels)
                            _monitor_wake.set()
                            _sse_broadcast("action")
                    threading.Thread(
                        target=_finish_quality_rollover,
                        args=(channel_name, rec, quality, platform_for_channel, quality_lock),
                        daemon=True, name=f"quality-rollover-{channel_name}",
                    ).start()
                    _sse_broadcast("action")
                    return jsonify({"ok": True, "restarted": True, "msg": f"Qualità aggiornata a {quality}: sto finalizzando il file precedente e avvierò una nuova registrazione."})

                with _lock:
                    current = next((item for item in _channels if item["name"] == channel_name), None)
                    if current is not None and _recorders.get(channel_name) is rec:
                        _recorders[channel_name] = Recorder(channel_name, quality, platform_for_channel)
                        should_record = bool(current.get("is_recording", False) and not current.get("sleeping", False))
                        if should_record:
                            _force_check.add(channel_name)
                            _next_check_due[channel_name] = 0.0
                        _save_channels(_channels)
            else:
                with _lock:
                    current = next((item for item in _channels if item["name"] == channel_name), None)
                    if current and current.get("is_recording", False) and not current.get("sleeping", False):
                        _force_check.add(channel_name)
                        _next_check_due[channel_name] = 0.0
                    _save_channels(_channels)

        _monitor_wake.set()
        _sse_broadcast("action")
        if was_recording:
            return jsonify({"ok": True, "restarted": True, "msg": f"Qualità impostata su {quality}: la vecchia sessione è stata finalizzata e ne partirà una nuova."})
        return jsonify({"ok": True, "restarted": False, "msg": f"Qualità impostata su {quality}."})

    elif action == "pause_all":
        recs_to_stop = []
        with _lock:
            for ch in _channels:
                _clear_single_live_state(ch)
                ch["is_recording"] = False
                rec = _recorders.get(ch["name"])
                if rec and rec.is_recording:
                    recs_to_stop.append(rec)
            _save_channels(_channels)
        for rec in recs_to_stop:
            rec.stop()
        _monitor_wake.set()
        _sse_broadcast("action")
        return jsonify({"ok": True})

    elif action == "resume_all":
        with _lock:
            for ch in _channels:
                if ch.get("sleeping", False):
                    continue
                _clear_single_live_state(ch)
                ch["is_recording"] = True
                _force_check.add(ch["name"])
                _next_check_due[ch["name"]] = 0.0
            _save_channels(_channels)
        _monitor_wake.set()
        _sse_broadcast("action")
        return jsonify({"ok": True})

    return jsonify({"ok": False, "msg": "Azione non valida."})


@app.route("/api/delete_recording", methods=["POST"])
def api_delete_recording():
    data = request.get_json(silent=True) or {}
    filename = data.get("filename", "")
    parts = Path(filename).parts
    if len(parts) > 2 or ".." in parts:
        return jsonify({"ok": False, "msg": "Path non valido."}), 400
    path = os.path.join(RECORDINGS_DIR, filename)
    if not os.path.isfile(path):
        return jsonify({"ok": False, "msg": "File non trovato."}), 404
    if os.path.abspath(path) in _active_file_paths():
        return jsonify({"ok": False, "msg": "Impossibile eliminare: registrazione in corso."})
    try:
        os.remove(path)
    except OSError as exc:
        return jsonify({"ok": False, "msg": str(exc)})
    _sse_broadcast("action")
    return jsonify({"ok": True})


@app.route("/health")
def health():
    status = _build_status_json()["stats"]
    healthy = status["monitor_alive"] and status["monitor_state"] != "stopped"
    return jsonify({
        "status": "ok" if healthy else "degraded",
        "monitor_alive": status["monitor_alive"],
        "monitor_state": status["monitor_state"],
        "monitor_last_cycle": status["monitor_last_cycle"],
        "monitor_inflight": status["monitor_inflight"],
        "monitor_workers": status["monitor_workers"],
    }), 200 if healthy else 503


# ─── Shutdown ─────────────────────────────────────────────────────────────────

def _shutdown(sig, _frame):
    logger.info("Segnale %s — chiusura…", sig)
    for rec in list(_recorders.values()):
        if rec.is_recording:
            rec.stop()
    _online_pool.shutdown(wait=False)
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
