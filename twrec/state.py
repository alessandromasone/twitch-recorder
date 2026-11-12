"""Stato condiviso dell'applicazione e persistenza di channels.json.

Tutte le strutture mutabili vivono qui; i moduli le modificano solo tenendo `lock`.
"""

import atexit
import glob
import json
import os
import threading
import time
from datetime import datetime, timezone

from .config import BACKUP_DIR, CHANNELS_BACKUPS, CHANNELS_FILE, STREAM_QUALITY, logger
from .util import as_bool, coerce_timestamp, iso_timestamp, norm_platform, write_json_atomic

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


class _Unreadable(Exception):
    pass


def _parse_channels_file(path: str) -> list[dict]:
    """Legge una lista di canali; solleva _Unreadable se il file è vuoto, troncato o non valido."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _Unreadable(str(exc)) from None
    if isinstance(data, dict) and isinstance(data.get("channels"), list):
        data = data["channels"]   # formato dei backup
    if not isinstance(data, list):
        raise _Unreadable("il contenuto non è una lista di canali")
    return [normalize_channel(dict(ch)) for ch in data if isinstance(ch, dict) and ch.get("name")]


def load_channels() -> list[dict]:
    """Carica channels.json. Se è illeggibile (per esempio dopo uno spegnimento improvviso)
    lo mette da parte e recupera la lista dalla copia più recente dello storico."""
    if not os.path.exists(CHANNELS_FILE):
        return []
    try:
        return _parse_channels_file(CHANNELS_FILE)
    except _Unreadable as exc:
        logger.error("%s non è leggibile (%s): provo a recuperarlo", CHANNELS_FILE, exc)
    damaged = f"{CHANNELS_FILE}.danneggiato-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(CHANNELS_FILE, damaged)
        logger.warning("File danneggiato conservato come %s", os.path.basename(damaged))
    except OSError:
        pass
    # Un salvataggio interrotto può aver lasciato un temporaneo completo, più recente dei backup.
    candidates = sorted(glob.glob(CHANNELS_FILE + ".*.tmp") + glob.glob(CHANNELS_FILE + ".tmp"),
                        key=os.path.getmtime, reverse=True)
    candidates += [b["path"] for b in list_backups()]
    for path in candidates:
        try:
            channels_found = _parse_channels_file(path)
        except _Unreadable:
            continue
        logger.warning("Lista canali recuperata da %s (%d canali)", os.path.basename(path), len(channels_found))
        return channels_found
    logger.error("Nessuna copia valida trovata: si riparte con una lista vuota")
    return []


# ── Storico (backup) ──
# Ogni volta che cambia la lista dei canali o il loro stato (nome, piattaforma, qualità,
# registrazione, a dormire, Una volta, rerun) si salva una copia; restano le ultime
# CHANNELS_BACKUPS. Gli aggiornamenti dei controlli (ultimo controllo, ultima live…) non creano copie.

BACKUP_FIELDS = ("name", "platform", "quality", "is_recording", "sleeping", "single_live_mode", "rerun_policy")
_last_backup: list | None = None


def _essentials(data: list[dict]) -> list[dict]:
    return [{k: ch.get(k) for k in BACKUP_FIELDS} for ch in data]


def list_backups() -> list[dict]:
    """Copie dello storico, dalla più recente."""
    try:
        names = [n for n in os.listdir(BACKUP_DIR) if n.startswith("channels-") and n.endswith(".json")]
    except OSError:
        return []
    out = []
    for name in sorted(names, reverse=True):
        path = os.path.join(BACKUP_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            items = data.get("channels") if isinstance(data, dict) else None
            if not isinstance(items, list):
                continue
        except (OSError, ValueError):
            continue
        out.append({"id": name, "path": path, "saved_at": data.get("saved_at"), "channels": items})
    return out


def read_backup(backup_id: str) -> list[dict] | None:
    if os.path.basename(backup_id) != backup_id:
        return None
    return next((b["channels"] for b in list_backups() if b["id"] == backup_id), None)


def _write_backup(payload: list[dict]) -> None:
    global _last_backup
    if CHANNELS_BACKUPS <= 0:
        return
    essentials = _essentials(payload)
    if essentials == _last_backup:
        return
    now = datetime.now(timezone.utc)
    name = f"channels-{now.strftime('%Y%m%d-%H%M%S-%f')}.json"
    data = {"saved_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "channels": essentials}
    if not write_json_atomic(os.path.join(BACKUP_DIR, name), data):
        return
    _last_backup = essentials
    for old in list_backups()[CHANNELS_BACKUPS:]:
        try:
            os.remove(old["path"])
        except OSError:
            pass


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
        if write_json_atomic(CHANNELS_FILE, payload):
            _write_backup(payload)


def _saver_loop() -> None:
    while True:
        _dirty.wait()
        time.sleep(2)
        try:
            flush()
        except Exception as exc:  # pragma: no cover - difensivo
            logger.error("Salvataggio canali non riuscito: %s", exc)


def init() -> None:
    global _last_backup
    latest = list_backups()[:1]
    _last_backup = latest[0]["channels"] if latest else None
    channels[:] = load_channels()
    for leftover in glob.glob(CHANNELS_FILE + ".*.tmp") + glob.glob(CHANNELS_FILE + ".tmp"):
        try:
            os.remove(leftover)   # temporanei di salvataggi interrotti (già considerati sopra)
        except OSError:
            pass
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
