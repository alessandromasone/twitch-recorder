"""File delle registrazioni: nomi, indice in cache, metadati, miniature, dedup e conversione."""

import hashlib
from collections import OrderedDict
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime

from . import state
from .config import (
    AUTO_CONVERT, DEDUP_BYTES, DEDUP_ENABLED, DEDUP_FILE, DELETE_AFTER_CONVERT,
    FILE_EXTENSION, FILENAME_FORMAT, RECORDING_THUMBS, RECORDINGS_DIR,
    LEGACY_META_FILE, LEGACY_THUMBS_DIR, RECORDING_THUMBS_CACHE, RECORDINGS_META_FILE, VIDEO_EXTS, logger,
)
from .util import human_size, read_json, write_json_atomic

_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")

# ── Nomi file ──

_filename_lock = threading.Lock()
_reserved: set[str] = set()


def generate_filename(channel_name: str) -> str:
    """Riserva un nome univoco per ogni sessione, anche con cambi qualità ravvicinati."""
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    desired = FILENAME_FORMAT.format(name=channel_name, timestamp=ts, ext=FILE_EXTENSION)
    with _filename_lock:
        candidate = desired
        stem, ext = os.path.splitext(desired)
        suffix = 2
        while candidate in _reserved or os.path.exists(os.path.join(RECORDINGS_DIR, candidate)):
            candidate = f"{stem}_{suffix}{ext}"
            suffix += 1
        _reserved.add(candidate)
        return candidate


def safe_recording_path(filename: str) -> str | None:
    """Percorso di un file direttamente dentro RECORDINGS_DIR, oppure None se non valido."""
    name = os.path.basename(filename or "")
    if not name or name != filename or name.startswith("."):
        return None
    return os.path.join(RECORDINGS_DIR, name)


# ── Metadati delle registrazioni (titolo, categoria, rerun) ──

_meta_lock = threading.Lock()
_meta: dict[str, dict] = {}
_meta_loaded = False


def _load_meta() -> None:
    global _meta_loaded
    if not _meta_loaded:
        data = read_json(RECORDINGS_META_FILE, {})
        _meta.update(data if isinstance(data, dict) else {})
        _meta_loaded = True


def update_meta(filename: str, **fields) -> None:
    with _meta_lock:
        _load_meta()
        entry = _meta.setdefault(filename, {})
        changed = False
        for key, value in fields.items():
            if entry.get(key) != value:
                entry[key] = value
                changed = True
        if changed:
            write_json_atomic(RECORDINGS_META_FILE, _meta, indent=None)
    if changed:
        index.invalidate()


def get_meta(filename: str) -> dict:
    with _meta_lock:
        _load_meta()
        return dict(_meta.get(filename, {}))


def drop_meta(filename: str) -> None:
    with _meta_lock:
        _load_meta()
        if _meta.pop(filename, None) is not None:
            write_json_atomic(RECORDINGS_META_FILE, _meta, indent=None)


# ── Indice delle registrazioni in cache ──

def active_files() -> dict[str, float]:
    """name -> ora di inizio per i file attualmente in scrittura."""
    active: dict[str, float] = {}
    for rec in list(state.recorders.values()):
        path = rec.output_path
        if rec.is_recording and path:
            active[os.path.basename(path)] = float(rec.started_at or time.time())
    return active


def connecting_files() -> set[str]:
    """File appena creati da registrazioni che non hanno ancora ricevuto dati: non si mostrano."""
    names = set()
    for rec in list(state.recorders.values()):
        path = rec.output_path
        if rec.is_recording and path and not rec._last_data:
            names.add(os.path.basename(path))
    return names


def _start_ts_from_name(name: str) -> float | None:
    match = _TS_RE.search(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d_%H-%M-%S").timestamp()
    except ValueError:
        return None


def _channel_from_name(name: str) -> str:
    stem = os.path.splitext(name)[0]
    match = _TS_RE.search(stem)
    if match:
        return stem[:match.start()].rstrip("_- ") or stem
    return stem


class RecordingsIndex:
    """Evita di scansionare la cartella a ogni aggiornamento: rescan solo se invalidato o ogni 30 s."""

    RESCAN_SECONDS = 30

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: dict[str, dict] = {}
        self._signature = None
        self._last_scan = 0.0
        self._dirty = True
        self.version = 0

    def invalidate(self) -> None:
        self._dirty = True
        state.request_broadcast()

    def _scan(self, active: dict[str, float], hidden: set[str] = frozenset()) -> None:
        files: dict[str, dict] = {}
        try:
            entries = list(os.scandir(RECORDINGS_DIR))
        except OSError:
            entries = []
        for entry in entries:
            name = entry.name
            if name.startswith(".") or os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
                continue
            if name in hidden:
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            start = _start_ts_from_name(name)
            files[name] = {
                "name": name,
                "channel": _channel_from_name(name),
                "size": st.st_size,
                "modified_ts": st.st_mtime,
                "started_ts": start,
                "order_ts": start or active.get(name) or st.st_mtime,
            }
        for name, info in files.items():
            info.update({k: v for k, v in get_meta(name).items()
                         if k in ("title", "game", "rerun", "channel", "platform")})
            if info.get("channel") is None:
                info["channel"] = _channel_from_name(name)
        # La firma ignora la dimensione dei file attivi, che cambia di continuo.
        signature = tuple(sorted(
            (n, 0 if n in active else f["size"], 0 if n in active else int(f["modified_ts"]),
             repr(f.get("rerun")), f.get("title"))
            for n, f in files.items()
        )) + tuple(sorted(active))
        self._files = files
        self._last_scan = time.time()
        self._dirty = False
        if signature != self._signature:
            self._signature = signature
            self.version += 1

    def snapshot(self) -> tuple[int, list[dict], dict[str, int]]:
        """Ritorna (versione, lista ordinata, dimensioni correnti dei file attivi)."""
        active = active_files()
        hidden = connecting_files()
        with self._lock:
            if self._dirty or time.time() - self._last_scan > self.RESCAN_SECONDS \
                    or any(name not in self._files and name not in hidden for name in active):
                self._scan(active, hidden)
            now = time.time()
            out = []
            for name, info in self._files.items():
                item = dict(info)
                item["in_use"] = name in active
                end = now if item["in_use"] else item["modified_ts"]
                start = item.get("started_ts") or active.get(name)
                duration = end - start if start else None
                item["duration"] = round(duration) if duration and 0 < duration < 7 * 86400 else None
                item["size_human"] = human_size(item["size"])
                item["modified"] = datetime.fromtimestamp(item["modified_ts"]).strftime("%Y-%m-%d %H:%M")
                out.append(item)
            out.sort(key=lambda f: (f["order_ts"], f["name"].casefold()), reverse=True)
            sizes = {}
            for name in active:
                try:
                    sizes[name] = os.path.getsize(os.path.join(RECORDINGS_DIR, name))
                except OSError:
                    pass
            return self.version, out, sizes


index = RecordingsIndex()

_disk_cache: tuple[float, dict] = (0.0, {})


def disk_stats() -> dict:
    global _disk_cache
    if time.time() - _disk_cache[0] < 10 and _disk_cache[1]:
        return _disk_cache[1]
    try:
        total, used, free = shutil.disk_usage(RECORDINGS_DIR)
    except OSError:
        total = used = free = 0
    value = {
        "total": total, "used": used, "free": free,
        "total_human": human_size(total), "used_human": human_size(used),
        "free_human": human_size(free),
        "used_pct": round(used / total * 100, 1) if total else 0,
    }
    _disk_cache = (time.time(), value)
    return value


def has_free_space() -> bool:
    """Falso quando lo spazio libero è sotto MIN_FREE_GB: le nuove REC non partono."""
    from .config import MIN_FREE_GB
    if MIN_FREE_GB <= 0:
        return True
    free = disk_stats().get("free", 0)
    return not free or free >= MIN_FREE_GB * 1024 ** 3


def delete_recording(filename: str) -> tuple[bool, str]:
    path = safe_recording_path(filename)
    if not path:
        return False, "Percorso non valido."
    if not os.path.isfile(path):
        return False, "File non trovato."
    if os.path.basename(path) in active_files():
        return False, "Impossibile eliminare: registrazione in corso."
    try:
        os.remove(path)
    except OSError as exc:
        return False, str(exc)
    with _thumb_cache_lock:
        _thumb_cache.pop(os.path.basename(path), None)
    drop_meta(os.path.basename(path))
    index.invalidate()
    return True, "File eliminato."


# ── Miniature ──

_thumb_sem = threading.Semaphore(1)


_thumb_cache_lock = threading.Lock()
# name -> (dimensione del file quando è stata creata, JPEG oppure None se non estraibile)
_thumb_cache: "OrderedDict[str, tuple[int, bytes | None]]" = OrderedDict()


def recording_thumb(filename: str) -> bytes | None:
    """Miniatura JPEG di una registrazione conclusa, generata al volo e tenuta solo in memoria.

    Nessun file viene scritto: dopo un riavvio la miniatura si rigenera alla prima richiesta.
    La cache contiene al massimo RECORDING_THUMBS_CACHE immagini (circa 15 KB l'una).
    """
    if not RECORDING_THUMBS:
        return None
    path = safe_recording_path(filename)
    if not path or not os.path.isfile(path):
        return None
    name = os.path.basename(path)
    if name in active_files():
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    with _thumb_cache_lock:
        cached = _thumb_cache.get(name)
        if cached and cached[0] == size:
            _thumb_cache.move_to_end(name)
            return cached[1]
    with _thumb_sem:
        image = None
        try:
            offset = 30 if size > 64 * 1024 * 1024 else 2
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-ss", str(offset), "-skip_frame", "nokey", "-i", path,
                 "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "6",
                 "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                capture_output=True, timeout=25,
            )
            if result.returncode == 0 and result.stdout:
                image = result.stdout
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("Miniatura non generata per %s: %s", name, exc)
    with _thumb_cache_lock:
        _thumb_cache[name] = (size, image)
        while len(_thumb_cache) > RECORDING_THUMBS_CACHE:
            _thumb_cache.popitem(last=False)
    return image


def cleanup_legacy_files() -> None:
    """La cartella delle registrazioni deve contenere solo video: migra o rimuove i file della 2.0."""
    if os.path.isfile(LEGACY_META_FILE):
        try:
            if not os.path.exists(RECORDINGS_META_FILE):
                os.makedirs(os.path.dirname(os.path.abspath(RECORDINGS_META_FILE)), exist_ok=True)
                shutil.move(LEGACY_META_FILE, RECORDINGS_META_FILE)
                logger.info("Metadati delle registrazioni spostati in %s", RECORDINGS_META_FILE)
            else:
                os.remove(LEGACY_META_FILE)
        except OSError as exc:
            logger.warning("Metadati delle registrazioni non spostati: %s", exc)
    if os.path.isdir(LEGACY_THUMBS_DIR):
        shutil.rmtree(LEGACY_THUMBS_DIR, ignore_errors=True)
        logger.info("Rimossa la vecchia cartella delle miniature %s", LEGACY_THUMBS_DIR)
    from .config import RECORDING_LOGS
    if not RECORDING_LOGS:
        # Log per canale disattivati: si eliminano anche quelli rimasti da prima.
        removed = 0
        try:
            for entry in os.scandir(RECORDINGS_DIR):
                if entry.is_file() and entry.name.startswith(".") and (entry.name.endswith(".log") or entry.name.endswith(".log.1")):
                    os.remove(entry.path)
                    removed += 1
        except OSError:
            pass
        if removed:
            logger.info("RECORDING_LOGS=false: eliminati %d vecchi log dei canali", removed)


def frame_from_bytes(data: bytes, timeout: float = 15, label: str = "") -> bytes | None:
    """Estrae un fotogramma JPEG da un frammento MPEG-TS (miniature live).

    Primo tentativo: solo fotogrammi chiave (veloce). Se non riesce, decodifica tutto e
    prende il primo I-frame. L'errore di FFmpeg viene registrato per la diagnosi.
    """
    if not data:
        return None
    attempts = (
        ["-skip_frame", "nokey", "-f", "mpegts", "-i", "pipe:0", "-frames:v", "1", "-vf", "scale=440:-2"],
        ["-f", "mpegts", "-i", "pipe:0", "-frames:v", "1", "-vf", "select='eq(pict_type,I)',scale=440:-2", "-vsync", "0"],
    )
    error = ""
    with _thumb_sem:
        for args in attempts:
            try:
                result = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *args,
                     "-q:v", "6", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                    input=data, capture_output=True, timeout=timeout,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                error = (result.stderr or b"").decode("utf-8", errors="replace").strip()[-300:]
            except (OSError, subprocess.TimeoutExpired) as exc:
                error = str(exc)
    logger.info("Miniatura live non estratta%s: %s", f" per {label}" if label else "", error or "nessun fotogramma")
    return None


# ── Deduplicazione e conversione (facoltative) ──

_dedup_lock = threading.Lock()
_convert_sem = threading.Semaphore(2)


def compute_file_hash(filepath: str, nbytes: int = DEDUP_BYTES) -> str | None:
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
                digest.update(fh.read())
            else:
                digest.update(b"head:")
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


def is_duplicate(channel_name: str, filepath: str) -> bool:
    if not DEDUP_ENABLED:
        return False
    fingerprint = compute_file_hash(filepath)
    if not fingerprint:
        return False
    with _dedup_lock:
        hashes = read_json(DEDUP_FILE, {})
        if not isinstance(hashes, dict):
            hashes = {}
        channel_hashes = [h for h in hashes.get(channel_name, []) if isinstance(h, str)]
        if fingerprint in channel_hashes:
            logger.info("Duplicato rilevato per %s: %s", channel_name, os.path.basename(filepath))
            return True
        channel_hashes.append(fingerprint)
        hashes[channel_name] = channel_hashes[-50:]
        write_json_atomic(DEDUP_FILE, hashes)
    return False


def _do_convert(ts_path: str) -> None:
    stem, ext = os.path.splitext(ts_path)
    if ext.lower() != ".ts":
        return
    mp4_path = stem + ".mp4"
    if os.path.exists(mp4_path):
        return
    with _convert_sem:
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-n",
                 "-i", ts_path, "-map", "0", "-c", "copy", "-movflags", "+faststart", mp4_path],
                capture_output=True, timeout=3600,
            )
            if result.returncode == 0 and os.path.isfile(mp4_path) and os.path.getsize(mp4_path) > 1024:
                logger.info("Conversione completata: %s", os.path.basename(mp4_path))
                meta = get_meta(os.path.basename(ts_path))
                if meta:
                    update_meta(os.path.basename(mp4_path), **meta)
                if DELETE_AFTER_CONVERT:
                    try:
                        os.remove(ts_path)
                        drop_meta(os.path.basename(ts_path))
                    except OSError as exc:
                        logger.warning("MP4 creato ma TS non eliminato (%s): %s", os.path.basename(ts_path), exc)
            else:
                details = (result.stderr or b"").decode("utf-8", errors="replace")[-300:]
                logger.error("Conversione MP4 fallita per %s: %s", os.path.basename(ts_path), details)
                if os.path.exists(mp4_path):
                    os.remove(mp4_path)
        except subprocess.TimeoutExpired:
            logger.error("Timeout durante la conversione di %s", os.path.basename(ts_path))
            try:
                os.remove(mp4_path)
            except OSError:
                pass
        except Exception as exc:
            logger.error("Errore conversione %s: %s", os.path.basename(ts_path), exc)
        finally:
            index.invalidate()


def finalize_recording_file(channel_name: str, filepath: str | None) -> None:
    """Processa solo un segmento già chiuso: conversione/dedup non toccano file aperti."""
    with _filename_lock:
        if filepath:
            _reserved.discard(os.path.basename(filepath))
    index.invalidate()
    if not filepath:
        return
    try:
        if not os.path.isfile(filepath):
            return
        if os.path.getsize(filepath) < 4096:
            # Segmento vuoto (live non partita): non lasciare file inutili.
            os.remove(filepath)
            drop_meta(os.path.basename(filepath))
            return
    except OSError:
        return
    if is_duplicate(channel_name, filepath):
        try:
            os.remove(filepath)
            drop_meta(os.path.basename(filepath))
            logger.warning("Registrazione duplicata eliminata: %s", os.path.basename(filepath))
        except OSError as exc:
            logger.error("Non riesco a eliminare il duplicato %s: %s", os.path.basename(filepath), exc)
        return
    if AUTO_CONVERT and filepath.lower().endswith(".ts"):
        threading.Thread(target=_do_convert, args=(filepath,), daemon=True,
                         name=f"convert-{os.path.basename(filepath)[:40]}").start()
