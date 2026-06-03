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
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect,
    url_for, send_from_directory, flash, jsonify,
    Response, abort,
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
PORT            = int(os.getenv("PORT", "5000"))
MAX_FILE_SIZE   = int(os.getenv("MAX_FILE_SIZE", str(int(1.8 * 1024**3))))
LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO").upper()

# ── Anti-spam ──
JITTER_MAX         = int(os.getenv("JITTER_MAX", "10"))
CHECK_PAUSED_ROOMS = os.getenv("CHECK_PAUSED_ROOMS", "false").lower() in ("1", "true", "yes", "on")

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
    "twitch":  {"label": "Twitch",  "url": "https://twitch.tv/{name}"},
    "kick":    {"label": "Kick",    "url": "https://kick.com/{name}"},
    "youtube": {"label": "YouTube", "url": "https://www.youtube.com/@{name}/live"},
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

# ─── Flask app ────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))

os.makedirs(RECORDINGS_DIR, exist_ok=True)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _generate_filename(channel_name: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return FILENAME_FORMAT.format(name=channel_name, timestamp=ts, ext=FILE_EXTENSION)


def _load_channels() -> list[dict]:
    if os.path.exists(CHANNELS_FILE):
        try:
            with open(CHANNELS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                for ch in data:
                    if "quality" not in ch:
                        ch["quality"] = STREAM_QUALITY
                    ch["platform"] = _norm_platform(ch.get("platform"))
                    ch.pop("online", None)
                return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Impossibile leggere %s: %s", CHANNELS_FILE, exc)
    return []


def _save_channels(data: list[dict]) -> None:
    tmp = CHANNELS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, CHANNELS_FILE)
    except OSError as exc:
        logger.error("Impossibile salvare %s: %s", CHANNELS_FILE, exc)


def _resolve_quality(channel_name: str, preferred: str, platform: str = DEFAULT_PLATFORM) -> str:
    try:
        result = subprocess.run(
            ["streamlink", _platform_url(platform, channel_name), "--json"],
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


def _is_channel_online(channel_name: str, platform: str = DEFAULT_PLATFORM) -> tuple[bool, bool]:
    """Ritorna (is_online, is_certain). is_certain=False su errori/timeout."""
    try:
        result = subprocess.run(
            ["streamlink", _platform_url(platform, channel_name), "--json"],
            capture_output=True, timeout=15,
        )
        is_online = result.returncode == 0 and b'"streams"' in result.stdout
        return is_online, True
    except subprocess.TimeoutExpired:
        logger.warning("⚠ %s — timeout check online", channel_name)
        return False, False
    except Exception as exc:
        logger.warning("⚠ %s — errore check online: %s", channel_name, exc)
        return False, False


def _file_info(path: str) -> dict:
    try:
        stat = os.stat(path)
        return {
            "name": os.path.basename(path),
            "size": stat.st_size,
            "size_human": _human_size(stat.st_size),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "modified_ts": stat.st_mtime,
        }
    except OSError:
        return {"name": os.path.basename(path), "size": 0,
                "size_human": "—", "modified": "—", "modified_ts": 0}


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


def _active_file_paths() -> set[str]:
    return {
        os.path.abspath(rec.output_path)
        for rec in _recorders.values()
        if rec.is_recording and rec.output_path
    }


def _list_recordings() -> list[dict]:
    files = []
    active = _active_file_paths()
    try:
        for name in os.listdir(RECORDINGS_DIR):
            ext = os.path.splitext(name)[1].lower()
            if ext in _VIDEO_EXTS:
                full = os.path.join(RECORDINGS_DIR, name)
                info = _file_info(full)
                info["in_use"] = os.path.abspath(full) in active
                files.append(info)
    except OSError:
        pass
    files.sort(key=lambda f: f["modified_ts"], reverse=True)
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


# ─── Recorder ────────────────────────────────────────────────────────────────

class Recorder:
    __slots__ = (
        "channel_name", "process", "output_path",
        "is_recording", "stop_requested", "_lock", "_thread", "_log_fh",
        "started_at", "quality", "_split_triggered", "platform",
    )

    def __init__(self, channel_name: str, quality: str = "best", platform: str = DEFAULT_PLATFORM):
        self.channel_name = channel_name
        self.quality = quality
        self.platform = _norm_platform(platform)
        self.process: subprocess.Popen | None = None
        self.output_path: str | None = None
        self.is_recording = False
        self.stop_requested = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._log_fh = None
        self.started_at: float | None = None
        self._split_triggered = False

    def start(self) -> None:
        with self._lock:
            if self.is_recording:
                return
            self.stop_requested = False
            self.is_recording = True
            self.started_at = time.time()
            self._thread = threading.Thread(
                target=self._manager_loop, daemon=True,
                name=f"rec-{self.channel_name}",
            )
            self._thread.start()
            logger.info("▶ Avviata registrazione: %s @ %s", self.channel_name, self.quality)
            _send_notification("rec_start", self.channel_name,
                               f"Registrazione avviata a {self.quality}")

    def stop(self) -> None:
        with self._lock:
            if not self.is_recording:
                return
            self.stop_requested = True
            proc = self.process
        if proc:
            try:
                proc.terminate()
            except OSError:
                pass
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

            self.output_path = os.path.join(
                RECORDINGS_DIR, _generate_filename(self.channel_name),
            )
            cmd = ["streamlink"]
            if self.platform == "twitch":
                cmd.append("--twitch-disable-ads")
            cmd += [
                _platform_url(self.platform, self.channel_name),
                resolved, "-o", self.output_path,
            ]
            log_path = os.path.join(RECORDINGS_DIR, f".{self.channel_name}.log")
            t0 = time.time()

            try:
                self._log_fh = open(log_path, "a", encoding="utf-8")
                self.process = subprocess.Popen(cmd, stdout=self._log_fh, stderr=self._log_fh)
                threading.Thread(
                    target=self._watch_size,
                    args=(self.process, self.output_path),
                    daemon=True,
                ).start()
                self.process.wait()
            except Exception as exc:
                logger.error("Errore streamlink %s: %s", self.channel_name, exc)
            finally:
                if self._log_fh:
                    self._log_fh.close()
                    self._log_fh = None
                self.process = None

            if self.stop_requested:
                break

            elapsed = time.time() - t0

            if self._split_triggered:
                logger.info("✂ %s: split dopo %s — riavvio",
                            self.channel_name, _fmt_duration(elapsed))
                time.sleep(1)
                continue

            if elapsed < 20:
                if error_start is None:
                    error_start = time.time()
                if time.time() - error_start > 180:
                    logger.error("Tolleranza errori superata per %s — stop.", self.channel_name)
                    break
                if self._smart_retry():
                    error_start = None
                    continue
                break
            else:
                error_start = None
                logger.info("Fine segmento %s dopo %s — riavvio.",
                            self.channel_name, _fmt_duration(elapsed))
                time.sleep(1)

        with self._lock:
            self.is_recording = False
            self.started_at = None
        _send_notification("rec_end", self.channel_name, "Registrazione terminata")
        _sse_broadcast("update")

    def _smart_retry(self) -> bool:
        for attempt in range(RECONNECT_TRIES):
            if self.stop_requested:
                return False
            wait = RECONNECT_WAIT + random.uniform(0, 10)
            logger.info("🔁 %s: riconnessione %d/%d — attendo %.0fs…",
                        self.channel_name, attempt + 1, RECONNECT_TRIES, wait)
            time.sleep(wait)
            if self.stop_requested:
                return False
            is_online, _ = _is_channel_online(self.channel_name, self.platform)
            if is_online:
                logger.info("✅ %s: stream ancora attivo, riprendo registrazione",
                            self.channel_name)
                return True
        logger.info("❌ %s: stream non più disponibile dopo %d tentativi",
                    self.channel_name, RECONNECT_TRIES)
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
            time.sleep(5)


# ─── Stato globale ────────────────────────────────────────────────────────────

_lock = threading.Lock()
_channels: list[dict] = _load_channels()
_recorders: dict[str, Recorder] = {
    ch["name"]: Recorder(ch["name"], ch.get("quality", STREAM_QUALITY),
                         ch.get("platform", DEFAULT_PLATFORM))
    for ch in _channels
}
_online_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="online-check")
_online_status: dict[str, bool] = {}
_last_checked: dict[str, float] = {}
_force_check: set[str] = set()
_sse_subscribers: list[queue.Queue] = []
_sse_lock = threading.Lock()
_monitor_heartbeat: float = 0.0


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
    with _lock:
        ch_list = []
        for ch in _channels:
            rec = _recorders.get(ch["name"])
            lc = _last_checked.get(ch["name"], 0)
            plat = _norm_platform(ch.get("platform"))
            ch_list.append({
                "name": ch["name"],
                "platform": plat,
                "platform_label": PLATFORMS[plat]["label"],
                "home_url": _platform_url(plat, ch["name"]),
                "online": _online_status.get(ch["name"], False),
                "is_recording": ch.get("is_recording", False),
                "actually_recording": rec.is_recording if rec else False,
                "uptime": round(rec.uptime) if rec else 0,
                "uptime_human": _fmt_duration(rec.uptime) if rec and rec.uptime > 0 else None,
                "quality": ch.get("quality", STREAM_QUALITY),
                "current_file": os.path.basename(rec.output_path) if rec and rec.output_path else None,
                "last_checked": round(time.time() - lc) if lc > 0 else None,
            })
    recordings = _list_recordings()
    disk = _disk_stats()
    total_rec_size = sum(r["size"] for r in recordings)
    return {
        "channels": ch_list,
        "recordings": recordings,
        "disk": disk,
        "quality_options": QUALITY_OPTIONS,
        "platforms": [{"key": k, "label": v["label"]} for k, v in PLATFORMS.items()],
        "config": {
            "reconnect_wait": RECONNECT_WAIT,
            "reconnect_tries": RECONNECT_TRIES,
            "notify_type": NOTIFY_TYPE or "none",
            "check_paused_rooms": CHECK_PAUSED_ROOMS,
        },
        "stats": {
            "total_channels": len(ch_list),
            "online_channels": sum(1 for c in ch_list if c["online"]),
            "active_recordings": sum(1 for c in ch_list if c["actually_recording"]),
            "total_files": len(recordings),
            "total_size": total_rec_size,
            "total_size_human": _human_size(total_rec_size),
            "monitor_alive": _monitor_thread.is_alive(),
        },
    }


# ─── Monitor ─────────────────────────────────────────────────────────────────

def _monitor_loop() -> None:
    global _monitor_heartbeat

    logger.info("🚀 Monitor avviato — %d canali, intervallo %ds",
                len(_channels), CHECK_INTERVAL)
    if NOTIFY_TYPE:
        logger.info("📢 Notifiche: type=%s, events=%s, photo=%s",
                    NOTIFY_TYPE, NOTIFY_EVENTS,
                    "✓" if TELEGRAM_PHOTO else "✗")
    else:
        logger.info("📢 Notifiche: disattivate (NOTIFY_TYPE vuoto)")

    cycle = 0
    while True:
        cycle += 1
        try:
            with _lock:
                snapshot = copy.deepcopy(_channels)
                priority = list(_force_check)
                _force_check.clear()

            if not snapshot:
                logger.info("── Ciclo #%d: nessun canale configurato, attendo %ds",
                            cycle, CHECK_INTERVAL)
                time.sleep(CHECK_INTERVAL)
                continue

            if CHECK_PAUSED_ROOMS:
                active = snapshot
            else:
                active = [ch for ch in snapshot if ch.get("is_recording", True)]
            skipped = len(snapshot) - len(active)

            # Aggiungi canali prioritari anche se in pausa
            if priority:
                priority_set = set(priority)
                paused_priority = [ch for ch in snapshot
                                   if ch["name"] in priority_set and ch not in active]
                active = paused_priority + active

            logger.info("── Ciclo #%d: check %d canali%s%s ──",
                        cycle, len(active),
                        f" (skip {skipped} in pausa)" if skipped else "",
                        f" ({len(priority)} prioritari)" if priority else "")

            futures = {
                _online_pool.submit(_is_channel_online, ch["name"],
                                    _norm_platform(ch.get("platform"))): ch["name"]
                for ch in active
            }
            results: dict[str, tuple[bool, bool]] = {}
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    results[name] = fut.result()
                except Exception:
                    results[name] = (False, False)

            online_map: dict[str, bool] = {}

            with _lock:
                for ch in _channels:
                    name = ch["name"]
                    if name not in results:
                        continue
                    is_online, is_certain = results[name]
                    was_online = _online_status.get(name, False)
                    _last_checked[name] = time.time()

                    if is_certain:
                        _online_status[name] = is_online
                        if is_online and not was_online:
                            _send_notification("online", name, "Il canale e' online!")
                        elif not is_online and was_online:
                            _send_notification("offline", name, "Il canale e' offline")
                    else:
                        is_online = was_online

                    online_map[name] = is_online

                    rec = _recorders.get(name)
                    if rec is None:
                        continue
                    rec.quality = ch.get("quality", STREAM_QUALITY)
                    should = ch.get("is_recording", False)
                    if should and is_online and not rec.is_recording:
                        rec.start()
                    elif (not should or not is_online) and rec.is_recording:
                        rec.stop()
                _save_channels(_channels)

            _monitor_heartbeat = time.time()

            n_online = sum(1 for v in online_map.values() if v)
            n_offline = len(online_map) - n_online
            online_names = [n for n, v in online_map.items() if v]
            logger.info(
                "── Ciclo #%d completato: %d online, %d offline%s",
                cycle, n_online, n_offline,
                f" — online: {', '.join(online_names)}" if online_names else "",
            )

            _sse_broadcast("cycle_complete")

        except Exception as exc:
            logger.error("Errore monitor ciclo #%d: %s", cycle, exc, exc_info=True)

        sleep_time = CHECK_INTERVAL + random.uniform(0, JITTER_MAX)
        time.sleep(sleep_time)


_monitor_thread = threading.Thread(target=_monitor_loop, daemon=True, name="monitor")
_monitor_thread.start()


# ─── Watchdog ─────────────────────────────────────────────────────────────────

def _watchdog_loop() -> None:
    global _monitor_thread
    while True:
        time.sleep(60)
        try:
            if not _monitor_thread.is_alive():
                logger.error("Monitor morto — riavvio!")
                _monitor_thread = threading.Thread(
                    target=_monitor_loop, daemon=True, name="monitor")
                _monitor_thread.start()
            elif _monitor_heartbeat > 0:
                stale = time.time() - _monitor_heartbeat
                if stale > CHECK_INTERVAL * 3 + 300:
                    logger.error("Monitor bloccato da %.0fs — riavvio!", stale)
                    _monitor_thread = threading.Thread(
                        target=_monitor_loop, daemon=True, name="monitor")
                    _monitor_thread.start()
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
                    entry = {"name": channel_name, "is_recording": True,
                             "quality": quality, "platform": platform}
                    _channels.append(entry)
                    _recorders[channel_name] = Recorder(channel_name, quality, platform)
                    _save_channels(_channels)
                    _force_check.add(channel_name)
                    flash(f"{channel_name} aggiunto.", "success")

        elif action in ("pause", "resume") and channel_name:
            with _lock:
                ch = next((c for c in _channels if c["name"] == channel_name), None)
                if ch:
                    rec = _recorders.get(channel_name)
                    if action == "pause":
                        ch["is_recording"] = False
                        if rec and rec.is_recording:
                            rec.stop()
                    else:
                        ch["is_recording"] = True
                    _save_channels(_channels)

        elif action == "remove" and channel_name:
            with _lock:
                rec = _recorders.pop(channel_name, None)
                if rec:
                    rec.stop()
                _channels = [c for c in _channels if c["name"] != channel_name]
                _online_status.pop(channel_name, None)
                _save_channels(_channels)

        elif action == "set_quality" and channel_name:
            with _lock:
                ch = next((c for c in _channels if c["name"] == channel_name), None)
                if ch:
                    ch["quality"] = quality
                    rec = _recorders.get(channel_name)
                    if rec:
                        rec.quality = quality
                    _save_channels(_channels)

        elif action == "pause_all":
            with _lock:
                for ch in _channels:
                    ch["is_recording"] = False
                    rec = _recorders.get(ch["name"])
                    if rec and rec.is_recording:
                        rec.stop()
                _save_channels(_channels)

        elif action == "resume_all":
            with _lock:
                for ch in _channels:
                    ch["is_recording"] = True
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


@app.route("/preview/<path:filename>")
def preview_recording(filename):
    safe = os.path.basename(filename)
    path = os.path.join(RECORDINGS_DIR, safe)
    if not os.path.isfile(path):
        abort(404)

    file_size = os.path.getsize(path)
    range_header = request.headers.get("Range")

    # Per le estensioni video usiamo SEMPRE il mime corretto: mimetypes.guess_type
    # su molti sistemi mappa ".ts" a un tipo errato (es. Qt Linguist/TypeScript).
    ext = os.path.splitext(safe)[1].lower()
    mime_map = {
        ".ts": "video/mp2t", ".mp4": "video/mp4",
        ".mkv": "video/x-matroska", ".webm": "video/webm",
        ".flv": "video/x-flv", ".avi": "video/x-msvideo",
        ".mov": "video/quicktime",
    }
    mime = mime_map.get(ext) or mimetypes.guess_type(safe)[0] or "application/octet-stream"

    if range_header:
        m = re.search(r"bytes=(\d+)-(\d*)", range_header)
        if not m:
            abort(416)
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else min(start + 2 * 1024 * 1024, file_size - 1)
        end = min(end, file_size - 1)
        if start >= file_size:
            abort(416)
        length = end - start + 1

        def gen_range():
            with open(path, "rb") as f:
                f.seek(start)
                rem = length
                while rem > 0:
                    chunk = f.read(min(8192, rem))
                    if not chunk:
                        break
                    rem -= len(chunk)
                    yield chunk

        return Response(gen_range(), status=206, mimetype=mime, headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
            "Accept-Ranges": "bytes",
        })

    def gen_full():
        with open(path, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                yield chunk

    return Response(gen_full(), status=200, mimetype=mime,
                    headers={"Content-Length": str(file_size), "Accept-Ranges": "bytes"})


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
                 "is_recording": ch.get("is_recording", False)} for ch in _channels]

    if fmt == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["name", "platform", "quality", "is_recording"])
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
                        "is_recording": bool(item.get("is_recording", True)),
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
                        "is_recording": str(row.get("is_recording", "true")).lower()
                            in ("true", "1", "yes"),
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
                         "is_recording": room["is_recording"], "platform": plat}
                _channels.append(entry)
                _recorders[name] = Recorder(name, room["quality"], plat)
                _force_check.add(name)
                existing.add(name)
                imported += 1
            _save_channels(_channels)

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
    with _lock:
        if room:
            _force_check.add(room)
        else:
            for ch in _channels:
                _force_check.add(ch["name"])
    return jsonify({"queued": len(_force_check)})


@app.route("/api/toggle_check_paused", methods=["POST"])
def api_toggle_check_paused():
    global CHECK_PAUSED_ROOMS
    CHECK_PAUSED_ROOMS = not CHECK_PAUSED_ROOMS
    logger.info("CHECK_PAUSED_ROOMS → %s", CHECK_PAUSED_ROOMS)
    _sse_broadcast("action")
    return jsonify({"check_paused_rooms": CHECK_PAUSED_ROOMS})


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
            entry = {"name": channel_name, "is_recording": True,
                     "quality": quality, "platform": platform}
            _channels.append(entry)
            _recorders[channel_name] = Recorder(channel_name, quality, platform)
            _save_channels(_channels)
            _force_check.add(channel_name)
        _sse_broadcast("action")
        return jsonify({"ok": True, "msg": f"{channel_name} aggiunto ({PLATFORMS[platform]['label']})."})

    elif action in ("pause", "resume") and channel_name:
        rec_to_stop = None
        with _lock:
            ch = next((c for c in _channels if c["name"] == channel_name), None)
            if ch:
                rec = _recorders.get(channel_name)
                if action == "pause":
                    ch["is_recording"] = False
                    if rec and rec.is_recording:
                        rec_to_stop = rec
                else:
                    ch["is_recording"] = True
                _save_channels(_channels)
        if rec_to_stop:
            rec_to_stop.stop()
        _sse_broadcast("action")
        return jsonify({"ok": True})

    elif action == "remove" and channel_name:
        rec_to_stop = None
        with _lock:
            rec_to_stop = _recorders.pop(channel_name, None)
            _channels = [c for c in _channels if c["name"] != channel_name]
            _online_status.pop(channel_name, None)
            _save_channels(_channels)
        if rec_to_stop:
            rec_to_stop.stop()
        _sse_broadcast("action")
        return jsonify({"ok": True})

    elif action == "set_quality" and channel_name:
        with _lock:
            ch = next((c for c in _channels if c["name"] == channel_name), None)
            if ch:
                ch["quality"] = quality
                rec = _recorders.get(channel_name)
                if rec:
                    rec.quality = quality
                _save_channels(_channels)
        _sse_broadcast("action")
        return jsonify({"ok": True})

    elif action == "pause_all":
        recs_to_stop = []
        with _lock:
            for ch in _channels:
                ch["is_recording"] = False
                rec = _recorders.get(ch["name"])
                if rec and rec.is_recording:
                    recs_to_stop.append(rec)
            _save_channels(_channels)
        for rec in recs_to_stop:
            rec.stop()
        _sse_broadcast("action")
        return jsonify({"ok": True})

    elif action == "resume_all":
        with _lock:
            for ch in _channels:
                ch["is_recording"] = True
            _save_channels(_channels)
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
    return jsonify({
        "status": "ok",
        "monitor_alive": _monitor_thread.is_alive(),
        "monitor_last_cycle": round(time.time() - _monitor_heartbeat) if _monitor_heartbeat > 0 else None,
    }), 200


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
