"""Preview live condivisa: una sola sorgente per canale, qualunque sia il numero di spettatori.

* REC attiva  → un FFmpeg legge il buffer della registrazione (nessuna nuova connessione).
* REC ferma   → un solo Streamlink+FFmpeg temporaneo, chiuso quando l'ultimo spettatore
                 esce (dopo PREVIEW_LINGER_SECONDS), senza scrivere file.
"""

import os
import subprocess
import threading
import time

from . import state, storage
from .checks import streamlink_base_command
from .config import (
    PREVIEW_IDLE_TIMEOUT_SECONDS, PREVIEW_LINGER_SECONDS, PREVIEW_STARTUP_TIMEOUT_SECONDS,
    STREAM_QUALITY, logger,
)
from .hub import LiveStreamHub
from .quality import fallback_list
from .recorder import pump_stderr, remux_command
from .util import norm_platform, platform_url

_lock = threading.Lock()
_sources: dict[str, "PreviewSource"] = {}


def _key(platform: str, room: str) -> str:
    return f"{norm_platform(platform)}:{room}"


def _kill_group(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    import signal
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
            pass
        try:
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass


class PreviewSource:
    def __init__(self, platform: str, room: str, mode: str, rec_hub: LiveStreamHub | None = None,
                 quality: str = STREAM_QUALITY):
        self.platform = norm_platform(platform)
        self.room = room
        self.mode = mode                         # shared | temporary
        self.rec_hub = rec_hub
        self.quality = quality
        self.hub = LiveStreamHub(max_bytes=2 * 1024 * 1024)
        self.viewers = 0
        self.ffmpeg: subprocess.Popen | None = None
        self.streamlink: subprocess.Popen | None = None
        self._linger_timer: threading.Timer | None = None
        self._stopped = False

    @property
    def alive(self) -> bool:
        return not self._stopped and not self.hub.closed

    def start(self) -> None:
        if self.mode == "shared":
            self.ffmpeg = subprocess.Popen(remux_command("error", fast_probe=True), stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           bufsize=0, start_new_session=True)
            threading.Thread(target=self._feed_from_rec, daemon=True, name=f"pv-feed-{self.room}").start()
        else:
            cmd = streamlink_base_command(self.platform) + [
                "--stdout", platform_url(self.platform, self.room), fallback_list(self.quality)]
            self.streamlink = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, bufsize=0, start_new_session=True)
            threading.Thread(target=pump_stderr, args=(self.streamlink, None), daemon=True,
                             name=f"pv-sl-{self.room}").start()
            self.ffmpeg = subprocess.Popen(remux_command("error", fast_probe=True), stdin=self.streamlink.stdout,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           bufsize=0, start_new_session=True)
            self.streamlink.stdout.close()
        threading.Thread(target=self._pump_output, daemon=True, name=f"pv-out-{self.room}").start()

    def _feed_from_rec(self) -> None:
        stdin = self.ffmpeg.stdin
        try:
            for payload in self.rec_hub.iter_chunks(from_start=True):
                if self._stopped or self.ffmpeg.poll() is not None:
                    break
                if payload:
                    stdin.write(payload)
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                stdin.close()
            except (OSError, ValueError):
                pass

    def _pump_output(self) -> None:
        stdout = self.ffmpeg.stdout
        try:
            while True:
                chunk = stdout.read(64 * 1024)
                if not chunk:
                    break
                self.hub.publish(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self.stop()

    def acquire(self) -> None:
        with _lock:
            self.viewers += 1
            if self._linger_timer:
                self._linger_timer.cancel()
                self._linger_timer = None
        state.request_broadcast()

    def release(self) -> None:
        with _lock:
            self.viewers = max(0, self.viewers - 1)
            if self.viewers == 0 and not self._stopped:
                linger = PREVIEW_LINGER_SECONDS if self.mode == "temporary" else 0
                if linger:
                    self._linger_timer = threading.Timer(linger, self._stop_if_unused)
                    self._linger_timer.daemon = True
                    self._linger_timer.start()
                else:
                    threading.Thread(target=self.stop, daemon=True).start()
        state.request_broadcast()

    def _stop_if_unused(self) -> None:
        with _lock:
            if self.viewers:
                return
        self.stop()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        with _lock:
            if _sources.get(_key(self.platform, self.room)) is self:
                _sources.pop(_key(self.platform, self.room), None)
        self.hub.close()
        _kill_group(self.ffmpeg)
        _kill_group(self.streamlink)
        state.request_broadcast()


def get_or_create(platform: str, room: str) -> PreviewSource:
    platform = norm_platform(platform)
    with state.lock:
        channel = state.find_channel(room)
        if channel is None or norm_platform(channel.get("platform")) != platform:
            raise LookupError(room)
        quality = channel.get("quality", STREAM_QUALITY)
        rec = state.recorders.get(room)
    rec_hub = None
    if rec is not None:
        with rec._lock:
            if rec.is_recording and rec.shared_stream and not rec.shared_stream.closed:
                rec_hub = rec.shared_stream
    key = _key(platform, room)
    with _lock:
        source = _sources.get(key)
        wanted_mode = "shared" if rec_hub else "temporary"
        if source and source.alive and (source.mode == wanted_mode or wanted_mode == "temporary"):
            return source
        old = source
        source = PreviewSource(platform, room, wanted_mode, rec_hub, quality)
        _sources[key] = source
    if old:
        old.stop()
    source.start()
    logger.info("Preview %s avviata per %s", "condivisa" if wanted_mode == "shared" else "temporanea", room)
    return source


def stream(source: PreviewSource):
    """Generatore HTTP per uno spettatore."""
    source.acquire()
    received = False
    last_data = time.time()
    try:
        for chunk in source.hub.iter_chunks(from_start=True, timeout=5):
            if chunk:
                received = True
                last_data = time.time()
                yield chunk
                continue
            limit = PREVIEW_IDLE_TIMEOUT_SECONDS if received else PREVIEW_STARTUP_TIMEOUT_SECONDS
            if time.time() - last_data > limit:
                logger.warning("Preview di %s senza dati per %ss: chiusura", source.room, limit)
                break
    except (GeneratorExit, BrokenPipeError, ConnectionResetError):
        pass
    finally:
        source.release()


def close_temporary(room: str) -> None:
    """Quando parte la REC, la preview temporanea si chiude: il player si ricollega al flusso condiviso."""
    with _lock:
        targets = [s for s in _sources.values() if s.room == room and s.mode == "temporary"]
    for source in targets:
        source.stop()


def preview_state(platform: str, room: str) -> dict:
    with _lock:
        source = _sources.get(_key(platform, room))
        if not source or not source.alive or not source.viewers:
            return {"active": False, "mode": "off", "connections": 0}
        return {"active": True, "mode": source.mode, "connections": source.viewers}


def active_count() -> int:
    with _lock:
        return sum(1 for s in _sources.values() if s.alive and s.viewers)


def stop_all() -> None:
    with _lock:
        sources = list(_sources.values())
    for source in sources:
        source.stop()


# ── Miniature live ──

_thumb_lock = threading.Lock()
_thumb_cache: dict[str, tuple[float, bytes]] = {}
THUMB_TTL = 90


def live_thumbnail(room: str) -> bytes | None:
    """Fotogramma recente dal flusso già acquisito (REC o preview). Nessuna nuova connessione."""
    now = time.time()
    with _thumb_lock:
        cached = _thumb_cache.get(room)
        if cached and now - cached[0] < THUMB_TTL:
            return cached[1]
    hub = None
    rec = state.recorders.get(room)
    if rec is not None and rec.is_recording and rec.shared_stream and not rec.shared_stream.closed:
        hub = rec.shared_stream
    else:
        with _lock:
            source = next((s for s in _sources.values() if s.room == room and s.alive), None)
        if source:
            hub = source.hub
    if hub is None:
        return cached[1] if cached else None
    image = storage.frame_from_bytes(hub.tail_bytes(3 * 1024 * 1024), label=room)
    if image:
        with _thumb_lock:
            _thumb_cache[room] = (now, image)
        return image
    # Estrazione non riuscita (per esempio durante una pubblicità): resta valida l'ultima buona.
    return cached[1] if cached else None
