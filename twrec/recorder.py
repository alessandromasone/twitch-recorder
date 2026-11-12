"""Registratore: Streamlink -> FFmpeg (remux in copia) -> file + buffer per la preview."""

import os
import random
import signal
import subprocess
import threading
import time

from . import notify, state, storage
from .checks import PRIVATE_PHRASES, check_channel, streamlink_base_command
from .config import (
    DEFAULT_PLATFORM, MAX_FILE_SIZE, RECONNECT_TRIES, RECONNECT_WAIT, RECORDING_LOG_MAX_MB,
    RECORDING_LOGS, RECORDING_STALL_SECONDS, RECORDINGS_DIR, SKIP_TWITCH_ADS, logger,
)
from .hub import LiveStreamHub
from .quality import fallback_list, parse_opened_quality
from .util import fmt_duration, human_size, norm_platform, platform_url

REMUX_ARGS = [
    "-map", "0:v?", "-map", "0:a?", "-c", "copy",
    "-avoid_negative_ts", "make_zero", "-mpegts_copyts", "0",
    "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "-muxpreload", "0",
    "-flush_packets", "1", "-f", "mpegts", "pipe:1",
]


def remux_command(loglevel: str = "warning", fast_probe: bool = False) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", loglevel, "-nostdin"]
    if fast_probe:
        cmd += ["-probesize", "1048576", "-analyzeduration", "1000000"]
    return cmd + ["-fflags", "+genpts", "-i", "pipe:0"] + REMUX_ARGS


def pump_stderr(proc: subprocess.Popen, log_fh, on_line=None) -> None:
    """Copia lo stderr di un processo nel log e inoltra ogni riga al callback."""
    stream = proc.stderr
    if stream is None:
        return
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if log_fh is not None:
                try:
                    log_fh.write(line + "\n")
                    log_fh.flush()
                except (OSError, ValueError):
                    pass
            if on_line:
                on_line(line)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def open_channel_log(channel_name: str):
    """Apre il log del canale in append, ruotandolo oltre RECORDING_LOG_MAX_MB. None se disattivato."""
    if not RECORDING_LOGS:
        return None
    path = os.path.join(RECORDINGS_DIR, f".{channel_name}.log")
    try:
        if os.path.getsize(path) > RECORDING_LOG_MAX_MB * 1024 * 1024:
            os.replace(path, path + ".1")
    except OSError:
        pass
    try:
        return open(path, "a", encoding="utf-8")
    except OSError as exc:
        logger.warning("Log di %s non scrivibile: %s", channel_name, exc)
        return None


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Segnale a tutto il gruppo del processo (avviato con start_new_session): così si chiudono
    anche i figli, come l'FFmpeg che Streamlink avvia per YouTube, che altrimenti terrebbero
    aperta la pipe e lascerebbero la registrazione appesa."""
    try:
        os.killpg(proc.pid, sig)
    except (OSError, ProcessLookupError):
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def terminate(proc: subprocess.Popen | None, wait: float = 3) -> None:
    """Chiude un processo e i suoi figli senza mai bloccarsi né sollevare eccezioni."""
    if proc is None or proc.poll() is not None:
        return
    _signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=wait)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass


def stall_limit(platform: str) -> int:
    """Secondi senza dati dopo cui la registrazione è considerata ferma (0 = mai)."""
    if not RECORDING_STALL_SECONDS:
        return 0
    if norm_platform(platform) == "twitch" and SKIP_TWITCH_ADS:
        # Con le pubblicità filtrate Streamlink non invia nulla per tutta la pausa pubblicitaria.
        return max(RECORDING_STALL_SECONDS, 300)
    return RECORDING_STALL_SECONDS


class Recorder:
    def __init__(self, channel_name: str, quality: str = "best", platform: str = DEFAULT_PLATFORM):
        self.channel_name = channel_name
        self.quality = quality
        self.resolved_quality: str | None = None
        self.platform = norm_platform(platform)
        self.process: subprocess.Popen | None = None
        self.source_process: subprocess.Popen | None = None
        self.output_path: str | None = None
        self.is_recording = False
        self.stop_requested = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.started_at: float | None = None
        self._split_triggered = False
        self.shared_stream: LiveStreamHub | None = None
        self.reconnect_attempts = 0
        self.last_error = ""
        self._last_data = 0.0

    # ── ciclo di vita ──

    def start(self) -> None:
        with self._lock:
            if self.is_recording:
                return
            self.stop_requested = False
            self.is_recording = True
            self.started_at = time.time()
            self.last_error = ""
            self._thread = threading.Thread(target=self._manager_loop, daemon=True,
                                            name=f"rec-{self.channel_name}")
            self._thread.start()
        logger.info("▶ Avviata registrazione: %s @ %s", self.channel_name, self.quality)
        notify.send("rec_start", self.channel_name, f"Registrazione avviata a {self.quality}")
        state.request_broadcast()

    def stop(self, wait: float = 0) -> None:
        """Ferma la registrazione. Con `wait` attende fino a quei secondi che il file sia chiuso."""
        with self._lock:
            if not self.is_recording:
                return
            self.stop_requested = True
            children = (self.source_process, self.process)
            hub = self.shared_stream
            thread = self._thread
        # Prima Streamlink (FFmpeg riceve EOF e chiude il file), poi FFmpeg se serve.
        for child in children:
            if child and child.poll() is None:
                _signal_group(child, signal.SIGTERM)
        if hub:
            hub.close()
        logger.info("⏹ Stop: %s", self.channel_name)
        if wait and thread and thread is not threading.current_thread():
            thread.join(timeout=wait)

    def heal_if_dead(self) -> bool:
        """Rete di sicurezza: se il thread di registrazione è morto ma lo stato dice ancora REC,
        chiude i processi rimasti e libera il canale per una nuova registrazione."""
        with self._lock:
            thread = self._thread
            if not self.is_recording or (thread is not None and thread.is_alive()):
                return False
            children = (self.process, self.source_process)
            hub = self.shared_stream
            self.process = self.source_process = self.shared_stream = None
            self.is_recording = False
            self.started_at = None
        logger.error("🩹 %s: registrazione rimasta appesa senza thread attivo — la chiudo", self.channel_name)
        for child in children:
            terminate(child)
        if hub:
            hub.close()
        storage.index.invalidate()
        state.request_broadcast()
        return True

    def _stall_watch(self, source: subprocess.Popen, process: subprocess.Popen, done: threading.Event) -> None:
        """Chiude Streamlink e FFmpeg se non arrivano dati per troppo tempo: un flusso morto con la
        connessione ancora aperta non deve lasciare la registrazione appesa per sempre."""
        limit = stall_limit(self.platform)
        if not limit:
            return
        while not done.wait(5):
            idle = time.time() - self._last_data
            if idle >= limit:
                logger.warning("📡 %s: nessun dato da %ds — chiudo e ricontrollo il canale",
                               self.channel_name, int(idle))
                self.last_error = f"Flusso fermo da {int(idle)} s: riconnessione"
                terminate(source)
                terminate(process)
                return

    @property
    def uptime(self) -> float:
        if self.started_at and self.is_recording:
            return time.time() - self.started_at
        return 0.0

    # ── registrazione ──

    def _on_streamlink_line(self, line: str) -> None:
        opened = parse_opened_quality(line)
        if opened:
            with self._lock:
                self.resolved_quality = opened
            logger.info("%s: qualità richiesta %s, Streamlink apre %s", self.channel_name, self.quality, opened)
            state.request_broadcast()
            return
        lowered = line.lower()
        if "error:" in lowered or any(p in lowered for p in PRIVATE_PHRASES):
            self.last_error = line.split("error:", 1)[-1].strip()[:300] or line[:300]

    def _write_session_meta(self, filename: str) -> None:
        meta = state.live_meta.get(self.channel_name) or {}
        rerun = state.rerun_info.get(self.channel_name)
        storage.update_meta(
            filename, channel=self.channel_name, platform=self.platform,
            title=meta.get("title") or None, game=meta.get("game") or None,
            rerun=_rerun_summary(rerun),
        )

    def refresh_meta(self) -> None:
        """Chiamato dal monitor quando titolo o valutazione rerun cambiano durante la REC."""
        path = self.output_path
        if path and self.is_recording:
            self._write_session_meta(os.path.basename(path))

    def _manager_loop(self) -> None:
        """Esegue la sessione e, qualunque cosa succeda, la chiude: un errore imprevisto non deve
        lasciare il canale bloccato in REC (che impedirebbe anche le registrazioni successive)."""
        try:
            self._session_loop()
        except Exception:
            logger.exception("Errore imprevisto nella registrazione di %s", self.channel_name)
        finally:
            with self._lock:
                hub = self.shared_stream
                self.shared_stream = None
                self.is_recording = False
                self.started_at = None
                children = (self.process, self.source_process)
                self.process = self.source_process = None
            if hub:
                hub.close()
            for child in children:
                terminate(child)
            notify.send("rec_end", self.channel_name, "Registrazione terminata")
            storage.index.invalidate()
            state.request_broadcast()

    def _session_loop(self) -> None:
        error_start: float | None = None
        while not self.stop_requested:
            with self._lock:
                self.resolved_quality = None
            self._split_triggered = False
            filename = storage.generate_filename(self.channel_name)
            output_path = os.path.join(RECORDINGS_DIR, filename)
            hub = LiveStreamHub()
            with self._lock:
                self.output_path = output_path
                self.shared_stream = hub

            streamlink_cmd = streamlink_base_command(self.platform) + [
                "--stdout", platform_url(self.platform, self.channel_name), fallback_list(self.quality),
            ]
            t0 = time.time()
            source = process = None
            exit_code = None
            log_fh = None
            written = 0
            segment_done = threading.Event()
            split_threshold = max(int(MAX_FILE_SIZE * 0.97), MAX_FILE_SIZE - 20 * 1024 * 1024) if MAX_FILE_SIZE > 0 else 0
            try:
                log_fh = open_channel_log(self.channel_name)
                with open(output_path, "wb", buffering=0) as out:
                    source = subprocess.Popen(streamlink_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, bufsize=0, start_new_session=True)
                    threading.Thread(target=pump_stderr, args=(source, log_fh, self._on_streamlink_line),
                                     daemon=True, name=f"sl-log-{self.channel_name}").start()
                    # Senza log, lo stderr di FFmpeg va scartato (non nei log del container).
                    process = subprocess.Popen(remux_command(), stdin=source.stdout, stdout=subprocess.PIPE,
                                               stderr=log_fh or subprocess.DEVNULL, bufsize=0, start_new_session=True)
                    source.stdout.close()
                    with self._lock:
                        self.source_process, self.process = source, process
                    if self.stop_requested:
                        terminate(process)
                        terminate(source)
                    self._write_session_meta(filename)
                    storage.index.invalidate()
                    self._last_data = time.time()
                    threading.Thread(target=self._stall_watch, args=(source, process, segment_done),
                                     daemon=True, name=f"rec-stall-{self.channel_name}").start()
                    stdout = process.stdout
                    # Si legge fino a EOF anche dopo Stop: FFmpeg scarica gli ultimi pacchetti.
                    while True:
                        chunk = stdout.read(64 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        hub.publish(chunk)
                        written += len(chunk)
                        self._last_data = time.time()
                        if split_threshold and written >= split_threshold and not self._split_triggered:
                            logger.info("Split %s (%s)", filename, human_size(written))
                            self._split_triggered = True
                            try:
                                process.terminate()
                            except OSError:
                                pass
                    try:
                        exit_code = process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        terminate(process)
                        exit_code = process.poll()
            except Exception as exc:
                self.last_error = f"Errore acquisizione/remux: {exc}"[:300]
                logger.error("Errore acquisizione/remux %s: %s", self.channel_name, exc)
            finally:
                segment_done.set()
                terminate(process)
                terminate(source)
                for proc in (process, source):
                    if proc is not None and proc.stdout is not None:
                        try:
                            proc.stdout.close()
                        except (OSError, ValueError):
                            pass
                hub.close()
                with self._lock:
                    if self.shared_stream is hub:
                        self.shared_stream = None
                    if self.process is process:
                        self.process = None
                    if self.source_process is source:
                        self.source_process = None
                    if self.output_path == output_path:
                        self.output_path = None
                if log_fh:
                    try:
                        log_fh.close()
                    except OSError:
                        pass
                storage.finalize_recording_file(self.channel_name, output_path)

            if self.stop_requested:
                break
            elapsed = time.time() - t0
            if self._split_triggered:
                logger.info("✂ %s: split dopo %s — riavvio", self.channel_name, fmt_duration(elapsed))
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
                break
            error_start = None
            self.reconnect_attempts = 0
            self.last_error = ""
            logger.info("Fine segmento %s dopo %s — riavvio.", self.channel_name, fmt_duration(elapsed))
            time.sleep(1)

    def _smart_retry(self) -> bool:
        for attempt in range(RECONNECT_TRIES):
            if self.stop_requested:
                return False
            wait = RECONNECT_WAIT + random.uniform(0, 10)
            self.reconnect_attempts = attempt + 1
            state.request_broadcast()
            logger.info("🔁 %s: riconnessione %d/%d — attendo %.0fs…",
                        self.channel_name, attempt + 1, RECONNECT_TRIES, wait)
            deadline = time.time() + wait
            while time.time() < deadline:
                if self.stop_requested:
                    return False
                time.sleep(min(1.0, deadline - time.time()) if deadline > time.time() else 0)
            result = check_channel(self.channel_name, self.platform)
            if result.online and result.certain:
                logger.info("✅ %s: stream ancora attivo, riprendo registrazione", self.channel_name)
                self.last_error = ""
                return True
            if not result.certain and result.error:
                self.last_error = f"Stato stream incerto: {result.error}"[:300]
        logger.info("❌ %s: stream non recuperato dopo %d tentativi", self.channel_name, RECONNECT_TRIES)
        if not self.last_error:
            self.last_error = f"Stream non recuperato dopo {RECONNECT_TRIES} tentativi"
        return False


def _rerun_summary(info: dict | None) -> dict | None:
    if not info or info.get("level", "none") == "none":
        return None
    return {"level": info["level"], "score": info.get("score", 0),
            "reasons": [r["text"] for r in info.get("reasons", []) if r.get("points")]}
