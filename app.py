"""
Twitch Recorder — Backend Flask
Registra automaticamente stream Twitch quando vanno online.
"""

import json
import os
import subprocess
import threading
import logging
import shutil
import time
import copy
import signal
import sys
import secrets
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import (
    Flask, render_template, request, redirect,
    url_for, send_from_directory, flash, jsonify,
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

# Video extensions riconosciute (per filtrare .log dalla lista)
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
                return json.load(fh)
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
    """Ritorna info su un file di registrazione."""
    try:
        stat = os.stat(path)
        return {
            "name": os.path.basename(path),
            "size": stat.st_size,
            "size_human": _human_size(stat.st_size),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        }
    except OSError:
        return {"name": os.path.basename(path), "size": 0, "size_human": "—", "modified": "—"}


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def _list_recordings() -> list[dict]:
    """Elenca solo file video dalla cartella recordings."""
    files = []
    try:
        for name in os.listdir(RECORDINGS_DIR):
            ext = os.path.splitext(name)[1].lower()
            if ext in _VIDEO_EXTS:
                files.append(_file_info(os.path.join(RECORDINGS_DIR, name)))
    except OSError:
        pass
    files.sort(key=lambda f: f["name"], reverse=True)
    return files


def _disk_stats() -> dict:
    total, used, free = shutil.disk_usage(RECORDINGS_DIR)
    return {
        "total": total,
        "used": used,
        "free": free,
        "total_human": _human_size(total),
        "used_human": _human_size(used),
        "free_human": _human_size(free),
        "used_pct": round(used / total * 100, 1) if total else 0,
    }


# ─── Recorder ────────────────────────────────────────────────────────────────

class Recorder:
    """Gestisce la registrazione di un singolo canale Twitch."""

    __slots__ = (
        "channel_name", "process", "output_path",
        "is_recording", "stop_requested", "_lock", "_thread", "_log_fh",
        "started_at",
    )

    def __init__(self, channel_name: str):
        self.channel_name = channel_name
        self.process: subprocess.Popen | None = None
        self.output_path: str | None = None
        self.is_recording = False
        self.stop_requested = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._log_fh = None
        self.started_at: float | None = None

    # ── public ──

    def start(self) -> None:
        with self._lock:
            if self.is_recording:
                return
            self.stop_requested = False
            self.is_recording = True
            self.started_at = time.time()
            self._thread = threading.Thread(
                target=self._manager_loop, daemon=True, name=f"rec-{self.channel_name}",
            )
            self._thread.start()
            logger.info("▶ Avviata registrazione: %s", self.channel_name)

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
        logger.info("⏹ Stop richiesto: %s", self.channel_name)

    @property
    def uptime(self) -> float:
        if self.started_at and self.is_recording:
            return time.time() - self.started_at
        return 0.0

    # ── private ──

    def _manager_loop(self) -> None:
        error_start: float | None = None

        while not self.stop_requested:
            self.output_path = os.path.join(
                RECORDINGS_DIR, _generate_filename(self.channel_name),
            )
            cmd = [
                "streamlink",
                "--twitch-disable-ads",
                f"https://twitch.tv/{self.channel_name}",
                STREAM_QUALITY,
                "-o", self.output_path,
            ]
            log_path = os.path.join(RECORDINGS_DIR, f".{self.channel_name}.log")
            t0 = time.time()

            try:
                self._log_fh = open(log_path, "a", encoding="utf-8")
                self.process = subprocess.Popen(
                    cmd, stdout=self._log_fh, stderr=self._log_fh,
                )
                # Thread dedicato al monitoraggio dimensione
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
                    logger.error(
                        "Superata tolleranza errori (180 s) per %s — stop.",
                        self.channel_name,
                    )
                    break
                logger.warning("Crash %s — riavvio tra 5 s…", self.channel_name)
                time.sleep(5)
            else:
                error_start = None
                logger.info("Split / fine per %s — riavvio immediato.", self.channel_name)
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
                    logger.info("File %s ha raggiunto il limite — split.", path)
                    proc.terminate()
                    return
            except OSError:
                pass
            time.sleep(5)


# ─── Stato globale ────────────────────────────────────────────────────────────

_lock = threading.Lock()
_channels: list[dict] = _load_channels()
_recorders: dict[str, Recorder] = {ch["name"]: Recorder(ch["name"]) for ch in _channels}

# Executor riusabile per i check online (evita di ricreare pool a ogni ciclo)
_online_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="online-check")


def _monitor_loop() -> None:
    """Thread che controlla periodicamente i canali e avvia/ferma le registrazioni."""
    while True:
        try:
            with _lock:
                snapshot = copy.deepcopy(_channels)

            # Check online in parallelo
            futures = {
                _online_pool.submit(_is_channel_online, ch["name"]): ch["name"]
                for ch in snapshot
            }
            online: dict[str, bool] = {}
            for fut in as_completed(futures):
                online[futures[fut]] = fut.result()

            # Aggiorna stato e gestisci recorder
            with _lock:
                for ch in _channels:
                    name = ch["name"]
                    ch["online"] = online.get(name, False)

                    rec = _recorders.get(name)
                    if rec is None:
                        continue

                    should_record = ch.get("is_recording", False)
                    if should_record and ch["online"] and not rec.is_recording:
                        rec.start()
                    elif (not should_record or not ch["online"]) and rec.is_recording:
                        rec.stop()

                _save_channels(_channels)

        except Exception as exc:
            logger.error("Errore nel ciclo di monitoraggio: %s", exc)

        time.sleep(CHECK_INTERVAL)


# Avvia il thread monitor
threading.Thread(target=_monitor_loop, daemon=True, name="monitor").start()


# ─── Rotte pagina ────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def index():
    global _channels

    if request.method == "POST":
        action = request.form.get("action", "")
        raw = request.form.get("channel", "").strip().lower()

        # Estrai nome canale da URL se necessario
        if "twitch.tv/" in raw:
            raw = raw.split("twitch.tv/")[-1].split("/")[0].split("?")[0]
        channel_name = raw

        if action == "add" and channel_name:
            with _lock:
                if any(c["name"] == channel_name for c in _channels):
                    flash("Canale già presente.", "warning")
                else:
                    entry = {"name": channel_name, "is_recording": True, "online": False}
                    _channels.append(entry)
                    _recorders[channel_name] = Recorder(channel_name)
                    _save_channels(_channels)
                    flash(f"Canale {channel_name} aggiunto.", "success")

        elif action in ("pause", "resume") and channel_name:
            with _lock:
                ch = next((c for c in _channels if c["name"] == channel_name), None)
                if ch is None:
                    flash("Canale non trovato.", "danger")
                else:
                    rec = _recorders.get(channel_name)
                    if action == "pause":
                        ch["is_recording"] = False
                        if rec and rec.is_recording:
                            rec.stop()
                        flash(f"{channel_name} messo in pausa.", "info")
                    else:
                        ch["is_recording"] = True
                        flash(f"{channel_name} ripreso.", "success")
                    _save_channels(_channels)

        elif action == "remove" and channel_name:
            with _lock:
                rec = _recorders.pop(channel_name, None)
                if rec:
                    rec.stop()
                _channels = [c for c in _channels if c["name"] != channel_name]
                _save_channels(_channels)
                flash(f"Canale {channel_name} rimosso.", "danger")

        return redirect(url_for("index"))

    # GET
    return render_template("index.html")


@app.route("/recordings/<path:filename>")
def download_recording(filename):
    return send_from_directory(RECORDINGS_DIR, filename)


@app.route("/delete_recording", methods=["POST"])
def delete_recording():
    filename = request.form.get("filename", "")
    safe = os.path.basename(filename)
    path = os.path.join(RECORDINGS_DIR, safe)
    if os.path.exists(path):
        try:
            os.remove(path)
            flash(f"File {safe} eliminato.", "success")
        except OSError as exc:
            flash(f"Errore: {exc}", "danger")
    else:
        flash("File non trovato.", "warning")
    return redirect(url_for("index"))


# ─── API JSON (per auto-refresh) ─────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    """Endpoint unificato per lo stato completo — usato dall'auto-refresh JS."""
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
            })

    recordings = _list_recordings()
    disk = _disk_stats()

    total_rec_size = sum(r["size"] for r in recordings)

    return jsonify({
        "channels": ch_list,
        "recordings": recordings,
        "disk": disk,
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
    return jsonify({"status": "ok", "uptime": time.time()}), 200


# ─── Graceful shutdown ───────────────────────────────────────────────────────

def _shutdown(sig, _frame):
    logger.info("Ricevuto segnale %s — chiusura…", sig)
    for rec in list(_recorders.values()):
        if rec.is_recording:
            rec.stop()
    _online_pool.shutdown(wait=False)
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    app.run(host="0.0.0.0", port=PORT)
