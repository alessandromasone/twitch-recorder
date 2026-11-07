"""Stato per API e dashboard, con aggiornamenti SSE incrementali.

Il primo messaggio SSE è lo stato completo; i successivi contengono solo i canali cambiati,
la lista registrazioni solo quando cambia e le statistiche. Senza client collegati non viene
calcolato nulla.
"""

import json
import queue
import threading
import time

from . import preview, rerun, state, storage
from .config import (
    AUTO_CONVERT, DEDUP_ENABLED, DELETE_AFTER_CONVERT, ERROR_CHECK_INTERVAL, HELIX_AVAILABLE,
    HELIX_ENABLED, MINIMAL_UI, MONITOR_WORKERS, NOTIFY_TYPE, OFFLINE_CHECK_INTERVAL, OFFLINE_CONFIRM_INTERVAL,
    OFFLINE_CONFIRMATIONS, ONLINE_CHECK_INTERVAL, PLATFORMS, QUALITY_OPTIONS, RECONNECT_TRIES,
    RECONNECT_WAIT, RECORDER_OFFLINE_GRACE_SECONDS, RERUN_DEFAULT_POLICY, RERUN_PROBABLE_SCORE,
    RERUN_SUSPECT_SCORE, SINGLE_LIVE_GRACE_SECONDS, SKIP_RERUNS, SKIP_TWITCH_ADS, STREAM_QUALITY,
)
from .util import fmt_duration, human_size, norm_platform, platform_url

VOLATILE = {"uptime", "uptime_human", "last_checked", "single_live_offline_remaining"}


def _thumb_url(name: str, platform: str, live: bool, recording: bool, meta: dict) -> str | None:
    """Copertina: per Twitch l'immagine ufficiale (API Helix o CDN), per le altre il flusso in REC."""
    if platform == "twitch" and live:
        template = meta.get("thumbnail_url") or f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{name}-{{width}}x{{height}}.jpg"
        return template.replace("{width}", "440").replace("{height}", "248")
    if recording:
        return f"/thumb/live/{name}.jpg"
    return None


def _channel_dict(ch: dict, now: float) -> dict:
    name = ch["name"]
    plat = norm_platform(ch.get("platform"))
    rec = state.recorders.get(name)
    if rec:
        with rec._lock:
            rec_is_recording = bool(rec.is_recording)
            rec_started = rec.started_at
            rec_stream = rec.shared_stream
            reconnect_attempts = rec.reconnect_attempts
            recorder_error = rec.last_error
            resolved_quality = rec.resolved_quality
            current_file = rec.output_path
    else:
        rec_is_recording, rec_started, rec_stream = False, None, None
        reconnect_attempts, recorder_error, resolved_quality, current_file = 0, "", None, None
    uptime = now - rec_started if rec_is_recording and rec_started else 0
    lc = state.last_checked.get(name, 0)
    online = bool(state.online_status.get(name, False) and not ch.get("sleeping", False))
    meta = state.live_meta.get(name) or {}
    profile = state.profiles.get(name) or {}
    assessment = state.rerun_info.get(name)
    pv = preview.preview_state(plat, name)
    live_now = online or ch.get("last_check_result") == "rerun"
    return {
        "name": name,
        "platform": plat,
        "platform_label": PLATFORMS[plat]["label"],
        "platform_short": PLATFORMS[plat]["short"],
        "platform_color": PLATFORMS[plat]["color"],
        "home_url": platform_url(plat, name),
        "display_name": profile.get("display_name") or meta.get("display_name") or name,
        "avatar": profile.get("avatar"),
        "online": online,
        "sleeping": bool(ch.get("sleeping", False)),
        "last_status": ch.get("last_status", "unknown"),
        "last_check_result": ch.get("last_check_result", "checking"),
        "check_failures": int(ch.get("check_failures", 0)),
        "offline_confirmation_count": state.offline_candidates.get(name, 0),
        "offline_confirmations_required": OFFLINE_CONFIRMATIONS,
        "last_error": ch.get("last_error") or recorder_error,
        "is_recording": bool(ch.get("is_recording", False)),
        "actually_recording": rec_is_recording,
        "rec_started_at": rec_started if rec_is_recording else None,
        "uptime": round(uptime),
        "uptime_human": fmt_duration(uptime) if uptime > 0 else None,
        "reconnect_attempts": reconnect_attempts,
        "quality": ch.get("quality", STREAM_QUALITY),
        "resolved_quality": resolved_quality,
        "single_live_mode": bool(ch.get("single_live_mode", False)),
        "single_live_seen_online": bool(ch.get("single_live_seen_online", False)),
        "single_live_offline_since": ch.get("single_live_offline_since") if ch.get("single_live_mode") else None,
        "single_live_offline_remaining": (
            max(0, int(SINGLE_LIVE_GRACE_SECONDS - (now - ch["single_live_offline_since"])))
            if ch.get("single_live_mode") and ch.get("single_live_offline_since") is not None else None
        ),
        "current_file": current_file.rsplit("/", 1)[-1] if current_file else None,
        "last_checked": round(now - lc) if lc > 0 else None,
        "last_checked_at": lc or None,
        "last_live": ch.get("last_live") or ch.get("last_online"),
        "last_online": ch.get("last_online"),
        "last_status_change": ch.get("last_status_change"),
        "title": meta.get("title") if live_now else None,
        "game": meta.get("game") if live_now else None,
        "viewers": meta.get("viewers") if live_now else None,
        "stream_started_at": meta.get("started_at") if live_now else None,
        "thumb_url": _thumb_url(name, plat, live_now, rec_is_recording, meta),
        # Fotogramma dal flusso in registrazione: usato per il file in corso nella lista registrazioni.
        "frame_url": f"/thumb/live/{name}.jpg" if rec_is_recording else None,
        "rerun": ({"level": assessment.get("level"), "score": assessment.get("score"),
                   "reasons": [r for r in assessment.get("reasons", []) if r.get("points")],
                   "override": assessment.get("override")}
                  if assessment and live_now else None),
        "rerun_policy": ch.get("rerun_policy"),
        "rerun_policy_effective": rerun.effective_policy(ch),
        "preview_active": pv["active"],
        "preview_mode": pv["mode"],
        "preview_connections": pv["connections"],
        "shared_stream_available": bool(rec_stream and not rec_stream.closed),
    }


def _config() -> dict:
    return {
        "reconnect_wait": RECONNECT_WAIT, "reconnect_tries": RECONNECT_TRIES,
        "notify_type": NOTIFY_TYPE or "none", "monitor_workers": MONITOR_WORKERS,
        "online_check_interval": ONLINE_CHECK_INTERVAL, "offline_check_interval": OFFLINE_CHECK_INTERVAL,
        "error_check_interval": ERROR_CHECK_INTERVAL, "offline_confirmations": OFFLINE_CONFIRMATIONS,
        "offline_confirm_interval": OFFLINE_CONFIRM_INTERVAL,
        "recorder_offline_grace_seconds": RECORDER_OFFLINE_GRACE_SECONDS,
        "auto_convert": AUTO_CONVERT, "delete_after_convert": DELETE_AFTER_CONVERT,
        "dedup_enabled": DEDUP_ENABLED, "skip_reruns": SKIP_RERUNS, "skip_twitch_ads": SKIP_TWITCH_ADS,
        "helix_available": HELIX_AVAILABLE, "helix_checks": HELIX_ENABLED,
        "rerun_default_policy": RERUN_DEFAULT_POLICY, "rerun_suspect_score": RERUN_SUSPECT_SCORE,
        "rerun_probable_score": RERUN_PROBABLE_SCORE, "default_quality": STREAM_QUALITY,
        "minimal_ui": MINIMAL_UI,
    }


def _stats(channels: list[dict], recordings: list[dict], now: float) -> dict:
    thread = state.monitor.thread
    monitor_alive = bool(thread and thread.is_alive())
    inflight = dict(state.monitor.inflight)
    oldest = max((now - t for t in inflight.values()), default=0)
    hb = now - state.monitor.heartbeat if state.monitor.heartbeat > 0 else None
    last_progress = state.monitor.last_progress
    monitor_state = "stopped" if not monitor_alive else (
        "degraded" if oldest > 45 or (hb is not None and hb > 45) else "ok")
    total_size = sum(r["size"] for r in recordings)
    return {
        "total_channels": len(channels),
        "online_channels": sum(1 for c in channels if c["online"]),
        "active_recordings": sum(1 for c in channels if c["actually_recording"]),
        "active_previews": preview.active_count(),
        "suspected_reruns": sum(1 for c in channels if c["rerun"] and c["rerun"]["level"] != "none"),
        "total_files": len(recordings),
        "total_size": total_size,
        "total_size_human": human_size(total_size),
        "monitor_alive": monitor_alive,
        "monitor_state": monitor_state,
        "monitor_last_cycle": round(now - last_progress) if last_progress > 0 else None,
        "monitor_heartbeat": round(hb) if hb is not None else None,
        "monitor_inflight": len(inflight),
        "monitor_inflight_oldest": round(oldest) if inflight else 0,
        "monitor_workers": MONITOR_WORKERS,
    }


def build_full() -> dict:
    now = time.time()
    with state.lock:
        channels = [_channel_dict(ch, now) for ch in state.channels]
    version, recordings, active_sizes = storage.index.snapshot()
    for item in recordings:
        if item["name"] in active_sizes:
            item["size"] = active_sizes[item["name"]]
            item["size_human"] = human_size(item["size"])
    return {
        "server_time": now,
        "channels": channels,
        "recordings": recordings,
        "recordings_version": version,
        "active_sizes": active_sizes,
        "disk": storage.disk_stats(),
        "quality_options": QUALITY_OPTIONS,
        "platforms": [{"key": k, "label": v["label"], "short": v["short"], "color": v["color"]}
                      for k, v in PLATFORMS.items()],
        "config": _config(),
        "stats": _stats(channels, recordings, now),
    }


# ── SSE ──

_subscribers: list[queue.Queue] = []
_sub_lock = threading.Lock()
_last_channels: dict[str, dict] = {}
_last_rec_version = -1
_last_disk: dict = {}


def _stable(ch: dict) -> dict:
    return {k: v for k, v in ch.items() if k not in VOLATILE}


def _encode(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def subscribe() -> tuple[queue.Queue, str]:
    """Registra un client e ritorna (coda, messaggio iniziale con lo stato completo)."""
    q: queue.Queue = queue.Queue(maxsize=30)
    full = build_full()
    with _sub_lock:
        _subscribers.append(q)
    return q, _encode("status", full)


def unsubscribe(q: queue.Queue) -> None:
    with _sub_lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


def _broadcast_once() -> None:
    global _last_rec_version, _last_disk
    with _sub_lock:
        if not _subscribers:
            return
    full = build_full()
    current = {c["name"]: _stable(c) for c in full["channels"]}
    changed = [c for c in full["channels"] if _last_channels.get(c["name"]) != current[c["name"]]]
    removed = [n for n in _last_channels if n not in current]
    delta = {
        "server_time": full["server_time"],
        "channels_changed": changed,
        "channels_removed": removed,
        "order": [c["name"] for c in full["channels"]],
        "stats": full["stats"],
        "active_sizes": full["active_sizes"],
    }
    if full["recordings_version"] != _last_rec_version:
        delta["recordings"] = full["recordings"]
        delta["recordings_version"] = full["recordings_version"]
    if full["disk"] != _last_disk:
        delta["disk"] = full["disk"]
    _last_channels.clear()
    _last_channels.update(current)
    _last_rec_version = full["recordings_version"]
    _last_disk = full["disk"]
    msg = _encode("delta", delta)
    with _sub_lock:
        for q in list(_subscribers):
            try:
                q.put_nowait(msg)
            except queue.Full:
                # Client troppo lento: viene scollegato e al riconnettersi riceve lo stato completo.
                _subscribers.remove(q)
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass


def _broadcaster_loop() -> None:
    while True:
        state.broadcast_wake.wait(timeout=15)
        state.broadcast_wake.clear()
        try:
            _broadcast_once()
        except Exception:  # pragma: no cover - difensivo
            import logging
            logging.getLogger("twitch-recorder").exception("Aggiornamento SSE non riuscito")
        time.sleep(0.4)   # al massimo ~2 aggiornamenti al secondo


def start() -> None:
    threading.Thread(target=_broadcaster_loop, daemon=True, name="sse-broadcaster").start()
