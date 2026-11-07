"""Stato condiviso dell'applicazione e persistenza di channels.json.

Tutte le strutture mutabili vivono qui; i moduli le modificano solo tenendo `lock`.
"""

import atexit
import threading
import time

from .config import CHANNELS_FILE, STREAM_QUALITY, logger
from .util import as_bool, coerce_timestamp, iso_timestamp, norm_platform, read_json, write_json_atomic

lock = threading.RLock()

channels: list[dict] = []
recorders: dict = {}                       # name -> Recorder
online_status: dict[str, bool] = {}
last_checked: dict[str, float] = {}
next_check_due: dict[str, float] = {}
offline_candidates: dict[str, int] = {}
offline_stop_due: dict[str, float] = {}
force_check: set[str] = set()
quality_change_locks: dict[str, threading.Lock] = {}
# Metadati dell'ultima live osservata (titolo, categoria, spettatori, avatar…)
live_meta: dict[str, dict] = {}
# Valutazione rerun della sessione corrente: name -> dict
rerun_info: dict[str, dict] = {}
# Profili Twitch (avatar, nome visualizzato) in cache: name -> dict
profiles: dict[str, dict] = {}


class MonitorStats:
    heartbeat: float = 0.0
    last_progress: float = 0.0
    inflight: dict[str, float] = {}
    thread: threading.Thread | None = None


monitor = MonitorStats()
monitor_wake = threading.Event()
profiles_wake = threading.Event()

# ── Aggiornamenti verso i client (SSE) ──
broadcast_wake = threading.Event()


def request_broadcast() -> None:
    """Richiede un aggiornamento dei client; più richieste ravvicinate vengono unite."""
    broadcast_wake.set()


def find_channel(name: str) -> dict | None:
    return next((c for c in channels if c.get("name") == name), None)


# ── Persistenza ──

TIMESTAMP_FIELDS = (
    "last_live", "last_online", "last_checked", "last_status_change",
    "single_live_offline_since",
)
_VALID_CHECK_RESULTS = ("online", "offline", "private", "verifying_offline", "error", "checking", "rerun")


def normalize_channel(ch: dict) -> dict:
    if "quality" not in ch:
        ch["quality"] = STREAM_QUALITY
    ch["platform"] = norm_platform(ch.get("platform"))
    ch["is_recording"] = as_bool(ch.get("is_recording", True), True)
    ch["sleeping"] = as_bool(ch.get("sleeping", False))
    if ch["sleeping"]:
        ch["is_recording"] = False
    ch["single_live_mode"] = as_bool(ch.get("single_live_mode", False))
    ch["single_live_seen_online"] = as_bool(ch.get("single_live_seen_online", False))
    ch["single_live_offline_since"] = coerce_timestamp(ch.get("single_live_offline_since"))
    if ch["sleeping"]:
        ch["single_live_mode"] = False
        ch["single_live_seen_online"] = False
        ch["single_live_offline_since"] = None
    ch["last_online"] = coerce_timestamp(ch.get("last_online"))
    ch["last_live"] = coerce_timestamp(ch.get("last_live") or ch.get("last_online"))
    ch["last_checked"] = coerce_timestamp(ch.get("last_checked"))
    ch["last_status_change"] = coerce_timestamp(ch.get("last_status_change"))
    if ch.get("last_status") not in ("online", "offline", "private", "rerun"):
        ch["last_status"] = "unknown"
    if ch.get("last_check_result") not in _VALID_CHECK_RESULTS:
        ch["last_check_result"] = "checking"
    try:
        ch["check_failures"] = max(0, int(ch.get("check_failures", 0)))
    except (TypeError, ValueError):
        ch["check_failures"] = 0
    ch["last_error"] = str(ch.get("last_error", "") or "")[:300]
    if ch.get("rerun_policy") not in ("record", "mark", "skip", None):
        ch["rerun_policy"] = None
    ch.setdefault("rerun_policy", None)
    ch.pop("online", None)
    return ch


def load_channels() -> list[dict]:
    data = read_json(CHANNELS_FILE, [])
    if not isinstance(data, list):
        logger.error("Il file %s deve contenere una lista JSON", CHANNELS_FILE)
        return []
    return [normalize_channel(ch) for ch in data if isinstance(ch, dict) and ch.get("name")]


def _serialize(data: list[dict]) -> list[dict]:
    out = []
    for channel in data:
        item = dict(channel)
        for field in TIMESTAMP_FIELDS:
            if field in item:
                item[field] = iso_timestamp(item.get(field))
        if item.get("last_live") is None and item.get("last_online") is not None:
            item["last_live"] = iso_timestamp(item["last_online"])
        out.append(item)
    return out


_dirty = threading.Event()
_save_lock = threading.Lock()


def mark_dirty() -> None:
    """Segnala che channels.json va riscritto. Le scritture sono raggruppate (max 1 ogni 2 s)."""
    _dirty.set()


def flush() -> None:
    with _save_lock:
        with lock:
            payload = _serialize(channels)
            _dirty.clear()
        write_json_atomic(CHANNELS_FILE, payload)


def _saver_loop() -> None:
    while True:
        _dirty.wait()
        time.sleep(2)
        try:
            flush()
        except Exception as exc:  # pragma: no cover - difensivo
            logger.error("Salvataggio canali non riuscito: %s", exc)


def init() -> None:
    channels[:] = load_channels()
    for ch in channels:
        online_status[ch["name"]] = ch.get("last_status") == "online"
        last_checked[ch["name"]] = float(ch.get("last_checked") or 0)
        next_check_due[ch["name"]] = 0.0
    if channels:
        flush()  # migra eventuali timestamp Unix al formato ISO-8601
    threading.Thread(target=_saver_loop, daemon=True, name="channels-saver").start()
    atexit.register(lambda: _dirty.is_set() and flush())


def forget_channel(name: str) -> None:
    """Rimuove ogni traccia runtime di un canale (chiamare tenendo `lock`)."""
    channels[:] = [c for c in channels if c.get("name") != name]
    for mapping in (online_status, last_checked, next_check_due, offline_candidates,
                    offline_stop_due, quality_change_locks, live_meta, rerun_info):
        mapping.pop(name, None)
    force_check.discard(name)
