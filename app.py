"""
Twitch Recorder — Backend Flask
Registra automaticamente stream Twitch quando vanno online.
"""

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
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# Qualità disponibili in ordine decrescente
QUALITY_OPTIONS = [
    "best", "1080p60", "1080p", "720p60", "720p",
    "480p", "360p", "160p", "worst", "audio_only",
]

_VIDEO_EXTS = {".ts", ".mp4", ".mkv", ".flv", ".avi", ".mov", ".webm"}

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


def _resolve_quality(channel_name: str, preferred: str) -> str:
    """
    Cerca la qualità preferita; se non disponibile, scala alla migliore
    disponibile partendo da quella richiesta verso il basso.
    """
    try:
        result = subprocess.run(
            ["streamlink", f"https://twitch.tv/{channel_name}", "--json"],
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

        # Cerca prima verso il basso (qualità inferiori)
        for q in QUALITY_OPTIONS[start_idx:]:
            if q in available:
                logger.info("%s: %s non disponibile, uso %s", channel_name, preferred, q)
                return q

        # Poi verso l'alto (qualità superiori)
        for q in reversed(QUALITY_OPTIONS[:start_idx]):
            if q in available:
                logger.info("%s: %s non disponibile, uso %s (superiore)", channel_name, preferred, q)
                return q

        return "best"
    except Exception:
        return preferred


def _is_channel_online(channel_name: str) -> bool:
    try:
        result = subprocess.run(
            ["streamlink", f"https://twitch.tv/{channel_name}", "--json"],
            capture_output=True, timeout=15,
        )
        return result.returncode == 0 and b'"streams"' in result.stdout
    except Exception:
        return False


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


def _list_recordings() -> list[dict]:
    files = []
    try:
        for name in os.listdir(RECORDINGS_DIR):
            ext = os.path.splitext(name)[1].lower()
            if ext in _VIDEO_EXTS:
                files.append(_file_info(os.path.join(RECORDINGS_DIR, name)))
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


# ─── Recorder ────────────────────────────────────────────────────────────────

class Recorder:
    __slots__ = (
        "channel_name", "process", "output_path",
        "is_recording", "stop_requested", "_lock", "_thread", "_log_fh",
        "started_at", "quality",
    )

    def __init__(self, channel_name: str, quality: str = "best"):
        self.channel_name = channel_name
        self.quality = quality
        self.process: subprocess.Popen | None = None
        self.output_path: str | None = None
        self.is_recording = False
        self.stop_requested = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._log_fh = None
        self.started_at: float | None = None

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
            resolved = _resolve_quality(self.channel_name, self.quality)

            self.output_path = os.path.join(
                RECORDINGS_DIR, _generate_filename(self.channel_name),
            )
            cmd = [
                "streamlink", "--twitch-disable-ads",
                f"https://twitch.tv/{self.channel_name}",
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
            if elapsed < 20:
                if error_start is None:
                    error_start = time.time()
                if time.time() - error_start > 180:
                    logger.error("Tolleranza errori superata per %s — stop.", self.channel_name)
                    break
                logger.warning("Crash %s — riavvio tra 5 s…", self.channel_name)
                time.sleep(5)
            else:
                error_start = None
                logger.info("Split/fine per %s — riavvio.", self.channel_name)
                time.sleep(1)

        with self._lock:
            self.is_recording = False
            self.started_at = None

    def _watch_size(self, proc: subprocess.Popen, path: str) -> None:
        if MAX_FILE_SIZE <= 0:
            return
        while not self.stop_requested:
            if proc.poll() is not None:
                return
            try:
                if os.path.exists(path) and os.path.getsize(path) >= MAX_FILE_SIZE:
                    logger.info("Split %s", path)
                    proc.terminate()
                    return
            except OSError:
                pass
            time.sleep(5)


# ─── Stato globale ────────────────────────────────────────────────────────────

_lock = threading.Lock()
_channels: list[dict] = _load_channels()
_recorders: dict[str, Recorder] = {
    ch["name"]: Recorder(ch["name"], ch.get("quality", STREAM_QUALITY))
    for ch in _channels
}
_online_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="online-check")


def _monitor_loop() -> None:
    while True:
        try:
            with _lock:
                snapshot = copy.deepcopy(_channels)

            futures = {
                _online_pool.submit(_is_channel_online, ch["name"]): ch["name"]
                for ch in snapshot
            }
            online: dict[str, bool] = {}
            for fut in as_completed(futures):
                online[futures[fut]] = fut.result()

            with _lock:
                for ch in _channels:
                    name = ch["name"]
                    ch["online"] = online.get(name, False)
                    rec = _recorders.get(name)
                    if rec is None:
                        continue
                    rec.quality = ch.get("quality", STREAM_QUALITY)
                    should = ch.get("is_recording", False)
                    if should and ch["online"] and not rec.is_recording:
                        rec.start()
                    elif (not should or not ch["online"]) and rec.is_recording:
                        rec.stop()
                _save_channels(_channels)
        except Exception as exc:
            logger.error("Errore monitor: %s", exc)
        time.sleep(CHECK_INTERVAL)


threading.Thread(target=_monitor_loop, daemon=True, name="monitor").start()


# ─── Rotte pagina ────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def index():
    global _channels

    if request.method == "POST":
        action = request.form.get("action", "")
        raw = request.form.get("channel", "").strip().lower()
        if "twitch.tv/" in raw:
            raw = raw.split("twitch.tv/")[-1].split("/")[0].split("?")[0]
        channel_name = raw
        quality = request.form.get("quality", STREAM_QUALITY)

        if action == "add" and channel_name:
            with _lock:
                if any(c["name"] == channel_name for c in _channels):
                    flash("Canale già presente.", "warning")
                else:
                    entry = {"name": channel_name, "is_recording": True,
                             "online": False, "quality": quality}
                    _channels.append(entry)
                    _recorders[channel_name] = Recorder(channel_name, quality)
                    _save_channels(_channels)
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
    return send_from_directory(RECORDINGS_DIR, filename)


@app.route("/preview/<path:filename>")
def preview_recording(filename):
    """Streaming video con supporto byte-range per seek nel browser."""
    safe = os.path.basename(filename)
    path = os.path.join(RECORDINGS_DIR, safe)
    if not os.path.isfile(path):
        abort(404)

    file_size = os.path.getsize(path)
    range_header = request.headers.get("Range")

    mime = mimetypes.guess_type(safe)[0]
    if mime is None:
        ext = os.path.splitext(safe)[1].lower()
        mime_map = {
            ".ts": "video/mp2t", ".mp4": "video/mp4",
            ".mkv": "video/x-matroska", ".webm": "video/webm",
            ".flv": "video/x-flv", ".avi": "video/x-msvideo",
        }
        mime = mime_map.get(ext, "application/octet-stream")

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
        try:
            os.remove(path)
        except OSError as exc:
            flash(f"Errore: {exc}", "danger")
    return redirect(url_for("index"))


# ─── API JSON ─────────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    with _lock:
        ch_list = []
        for ch in _channels:
            rec = _recorders.get(ch["name"])
            ch_list.append({
                "name": ch["name"],
                "online": ch.get("online", False),
                "is_recording": ch.get("is_recording", False),
                "actually_recording": rec.is_recording if rec else False,
                "uptime": round(rec.uptime) if rec else 0,
                "quality": ch.get("quality", STREAM_QUALITY),
                "current_file": os.path.basename(rec.output_path) if rec and rec.output_path else None,
            })
    recordings = _list_recordings()
    disk = _disk_stats()
    total_rec_size = sum(r["size"] for r in recordings)
    return jsonify({
        "channels": ch_list,
        "recordings": recordings,
        "disk": disk,
        "quality_options": QUALITY_OPTIONS,
        "stats": {
            "total_channels": len(ch_list),
            "online_channels": sum(1 for c in ch_list if c["online"]),
            "active_recordings": sum(1 for c in ch_list if c["actually_recording"]),
            "total_files": len(recordings),
            "total_size": total_rec_size,
            "total_size_human": _human_size(total_rec_size),
        },
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


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
    app.run(host="0.0.0.0", port=PORT)
