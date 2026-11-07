"""Monitor adattivo: pianifica i controlli, applica isteresi offline, avvia/ferma la REC."""

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from . import notify, preview, rerun, state, twitch_api
from .checks import CheckResult, check_streamlink, check_twitch_batch
from .config import (
    ERROR_CHECK_INTERVAL, HELIX_AVAILABLE, HELIX_ENABLED, HELIX_OFFLINE_CHECK_INTERVAL, MONITOR_WORKERS, OFFLINE_CHECK_INTERVAL,
    OFFLINE_CONFIRM_INTERVAL, OFFLINE_CONFIRMATIONS, ONLINE_CHECK_INTERVAL,
    RECORDER_OFFLINE_GRACE_SECONDS, SINGLE_LIVE_GRACE_SECONDS, STREAM_QUALITY, logger,
)
from .util import norm_platform

_pool = ThreadPoolExecutor(max_workers=MONITOR_WORKERS, thread_name_prefix="online-check")
_batch_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="helix-batch")
_helix_backoff_until = 0.0
_HELIX_FAILED = object()


# ── Modalità «Una volta» ──

def clear_single_live_state(channel: dict) -> None:
    channel["single_live_mode"] = False
    channel["single_live_seen_online"] = False
    channel["single_live_offline_since"] = None


def update_single_live_mode(channel: dict, is_online: bool, now: float | None = None) -> bool:
    """Aggiorna il timer «Una volta». Ritorna True se è scattata la pausa automatica."""
    if not channel.get("single_live_mode"):
        return False
    now = time.time() if now is None else now
    if is_online:
        channel["single_live_seen_online"] = True
        if channel.get("single_live_offline_since") is not None:
            channel["single_live_offline_since"] = None
            logger.info("▶ %s è tornato online durante l'attesa Una volta; timer azzerato", channel.get("name"))
        return False
    if not channel.get("single_live_seen_online"):
        return False
    offline_since = channel.get("single_live_offline_since")
    if offline_since is None:
        channel["single_live_offline_since"] = now
        logger.info("⏳ %s offline: attendo fino a 2 ore una nuova live prima della pausa automatica", channel.get("name"))
        return False
    if now - float(offline_since) >= SINGLE_LIVE_GRACE_SECONDS:
        channel["is_recording"] = False
        clear_single_live_state(channel)
        logger.info("⏸ %s messo in pausa automaticamente: nessuna nuova live entro 2 ore", channel.get("name"))
        return True
    return False


# ── Applicazione dei risultati ──

def _offline_rec_handling(name: str, ch: dict, rec, paused_by_once: bool, now: float):
    """Ritorna (intervallo, recorder da fermare) quando l'offline è confermato."""
    if not rec:
        return OFFLINE_CHECK_INTERVAL, None
    if paused_by_once or not ch.get("is_recording", False):
        state.offline_stop_due.pop(name, None)
        return OFFLINE_CHECK_INTERVAL, (rec if rec.is_recording else None)
    if rec.is_recording:
        due = state.offline_stop_due.get(name)
        if due is None:
            due = now + RECORDER_OFFLINE_GRACE_SECONDS
            state.offline_stop_due[name] = due
            logger.info("⏳ %s: offline confermato; attendo %ds prima di fermare REC", name, RECORDER_OFFLINE_GRACE_SECONDS)
        if now < due:
            return min(OFFLINE_CONFIRM_INTERVAL, max(1, int(due - now))), None
        state.offline_stop_due.pop(name, None)
        return OFFLINE_CHECK_INTERVAL, rec
    state.offline_stop_due.pop(name, None)
    return OFFLINE_CHECK_INTERVAL, None


def _set_confirmed_offline(name: str, ch: dict, old_status: str, now: float) -> None:
    state.offline_candidates.pop(name, None)
    state.online_status[name] = False
    ch["last_status"] = "offline"
    ch["last_check_result"] = "offline"
    ch["last_error"] = ""
    ch["check_failures"] = 0
    if old_status not in ("offline", "unknown"):
        ch["last_status_change"] = now
    if old_status in ("online", "rerun") or name in state.live_meta:
        rerun.on_offline(name, now)
    state.live_meta.pop(name, None)
    state.rerun_info.pop(name, None)


def apply_check_result(name: str, res: CheckResult) -> None:
    now = time.time()
    to_start = to_stop = None
    refresh_meta = None
    notification = None

    meta = assessment = None
    if res.online and res.certain:
        # Fuori dal lock globale: la valutazione può leggere le VOD da Helix (in cache).
        meta = dict(res.meta or {})
        meta["updated_at"] = now
        if not meta.get("user_id") and (state.profiles.get(name) or {}).get("id"):
            meta["user_id"] = state.profiles[name]["id"]
        assessment = rerun.on_live(name, meta, res.tagged_rerun, now)

    with state.lock:
        ch = state.find_channel(name)
        state.monitor.inflight.pop(name, None)
        if ch is None:
            return
        if ch.get("sleeping", False):
            state.offline_candidates.pop(name, None)
            state.offline_stop_due.pop(name, None)
            state.monitor.last_progress = now
            state.request_broadcast()
            return
        rec = state.recorders.get(name)
        old_status = ch.get("last_status", "unknown")
        was_online = bool(state.online_status.get(name, False) or old_status == "online")
        ch["last_checked"] = now
        state.last_checked[name] = now

        action = "record"
        if res.online and res.certain:
            previous_meta = state.live_meta.get(name) or {}
            state.live_meta[name] = meta
            previous_assessment = state.rerun_info.get(name)
            state.rerun_info[name] = assessment
            action = rerun.decide(ch, assessment)
            if rec and rec.is_recording and (
                    previous_meta.get("title") != meta.get("title")
                    or (previous_assessment or {}).get("level") != assessment.get("level")):
                refresh_meta = rec

        if not res.certain:
            ch["check_failures"] = int(ch.get("check_failures", 0)) + 1
            ch["last_error"] = (res.error or "Errore temporaneo durante il controllo")[:300]
            ch["last_check_result"] = "error"
            state.offline_candidates.pop(name, None)
            state.online_status[name] = was_online
            interval = ERROR_CHECK_INTERVAL
            if name in state.offline_stop_due:
                interval = min(interval, OFFLINE_CONFIRM_INTERVAL)

        elif res.online and action == "skip":
            # Rerun da saltare: non conta come live e non aggiorna last_live.
            state.offline_candidates.pop(name, None)
            state.offline_stop_due.pop(name, None)
            state.online_status[name] = False
            ch["last_status"] = "rerun"
            ch["last_check_result"] = "rerun"
            ch["last_error"] = ""
            ch["check_failures"] = 0
            if old_status != "rerun":
                ch["last_status_change"] = now
                logger.info("⏭ %s: rerun (%s, punteggio %s) — registrazione saltata", name,
                            state.rerun_info[name].get("level"), state.rerun_info[name].get("score"))
            update_single_live_mode(ch, False, now)
            if rec and rec.is_recording:
                to_stop = rec
            interval = ONLINE_CHECK_INTERVAL

        elif res.online:
            state.offline_candidates.pop(name, None)
            state.online_status[name] = True
            ch["last_status"] = "online"
            ch["last_check_result"] = "online"
            ch["last_error"] = ""
            ch["check_failures"] = 0
            ch["last_live"] = now
            ch["last_online"] = now
            if old_status != "online":
                ch["last_status_change"] = now
            if not was_online:
                title = (res.meta or {}).get("title")
                notification = ("online", name, f"Il canale è online!{' — ' + title if title else ''}")
            state.offline_stop_due.pop(name, None)
            update_single_live_mode(ch, True, now)
            interval = ONLINE_CHECK_INTERVAL
            if rec:
                rec.quality = ch.get("quality", STREAM_QUALITY)
                if ch.get("is_recording", False) and not rec.is_recording:
                    to_start = rec

        elif res.kind == "private":
            state.offline_candidates.pop(name, None)
            state.offline_stop_due.pop(name, None)
            state.online_status[name] = False
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
            # Un singolo esito negativo non ferma la REC: servono più conferme.
            had_confirmed_live = bool(
                was_online
                or (ch.get("single_live_seen_online") and ch.get("single_live_offline_since") is None)
            )
            needs_confirmation = bool(old_status != "offline" and (had_confirmed_live or (rec and rec.is_recording)))
            if needs_confirmation:
                count = state.offline_candidates.get(name, 0) + 1
                state.offline_candidates[name] = count
                if count < OFFLINE_CONFIRMATIONS:
                    state.online_status[name] = True
                    ch["last_check_result"] = "verifying_offline"
                    ch["last_error"] = ""
                    ch["check_failures"] = 0
                    interval = OFFLINE_CONFIRM_INTERVAL
                else:
                    _set_confirmed_offline(name, ch, old_status, now)
                    if old_status != "offline":
                        ch["last_status_change"] = now
                    if had_confirmed_live:
                        notification = ("offline", name, "Il canale è offline (confermato)")
                    paused = update_single_live_mode(ch, False, now)
                    interval, to_stop = _offline_rec_handling(name, ch, rec, paused, now)
            else:
                _set_confirmed_offline(name, ch, old_status, now)
                paused = update_single_live_mode(ch, False, now)
                interval, to_stop = _offline_rec_handling(name, ch, rec, paused, now)

        if res.via == "helix" and interval == OFFLINE_CHECK_INTERVAL:
            interval = min(interval, HELIX_OFFLINE_CHECK_INTERVAL)
        if ch.get("single_live_mode") and ch.get("single_live_offline_since") is not None:
            remaining = SINGLE_LIVE_GRACE_SECONDS - (now - float(ch["single_live_offline_since"]))
            interval = min(interval, OFFLINE_CONFIRM_INTERVAL if remaining <= 120 else max(1, int(remaining)))

        state.next_check_due[name] = now + max(1, interval)
        state.mark_dirty()

    if notification:
        notify.send(*notification)
    if to_stop:
        to_stop.stop()
    if refresh_meta:
        refresh_meta.refresh_meta()
    if to_start:
        with state.lock:
            current = state.find_channel(name)
            may_start = bool(current and current.get("is_recording") and not current.get("sleeping"))
        if may_start:
            preview.close_temporary(name)
            to_start.start()
            logger.info("▶ Avvio registrazione %s dopo controllo online confermato", name)
    state.monitor.last_progress = time.time()
    state.request_broadcast()


# ── Controlli ──

def _helix_batch(names: list[str]):
    global _helix_backoff_until
    try:
        results = check_twitch_batch(names)
    except Exception as exc:
        _helix_backoff_until = time.time() + 120
        logger.warning("Helix non disponibile (%s): per 2 minuti uso Streamlink", exc)
        return _HELIX_FAILED
    # Precarica (in cache per 3 h) le VOD dei canali live, usate dal confronto dei titoli.
    for res in results.values():
        if res.online and res.meta.get("user_id"):
            twitch_api.get_archive_videos(res.meta["user_id"])
    return results


def _profiles_loop() -> None:
    """Avatar e id dei canali Twitch (una richiesta ogni 100 canali, cache 24 h)."""
    while True:
        try:
            with state.lock:
                names = [c["name"] for c in state.channels if norm_platform(c.get("platform")) == "twitch"]
            missing = [n for n in names if n not in state.profiles]
            if missing:
                users = twitch_api.get_users(missing)
                for login, user in users.items():
                    if user:
                        state.profiles[login] = {"avatar": user.get("profile_image_url"),
                                                 "display_name": user.get("display_name"), "id": user.get("id")}
                if users:
                    state.request_broadcast()
        except Exception as exc:
            logger.debug("Profili Twitch non aggiornati: %s", exc)
        state.profiles_wake.wait(600)
        state.profiles_wake.clear()


def _monitor_loop() -> None:
    inflight: dict[Future, list[str]] = {}
    logger.info(
        "🚀 Monitor avviato — worker=%d, online=%ds, offline=%ds, errori=%ds, conferme offline=%d, Helix=%s",
        MONITOR_WORKERS, ONLINE_CHECK_INTERVAL, OFFLINE_CHECK_INTERVAL, ERROR_CHECK_INTERVAL,
        OFFLINE_CONFIRMATIONS, "sì" if HELIX_ENABLED else "no",
    )
    while True:
        cycle_now = time.time()
        try:
            for future, names in list(inflight.items()):
                if not future.done():
                    continue
                inflight.pop(future, None)
                try:
                    result = future.result()
                except Exception as exc:
                    result = CheckResult(False, False, "error", str(exc)[:300])
                if result is _HELIX_FAILED:
                    with state.lock:
                        for n in names:
                            state.monitor.inflight.pop(n, None)
                            state.next_check_due[n] = 0.0
                    continue
                if isinstance(result, dict):
                    for n in names:
                        apply_check_result(n, result.get(n) or CheckResult(False, False, "error", "Nessuna risposta"))
                else:
                    apply_check_result(names[0], result)

            with state.lock:
                forced = set(state.force_check)
                for n in forced:
                    state.next_check_due[n] = 0.0
                busy = {n for names in inflight.values() for n in names}
                eligible = []
                for ch in state.channels:
                    n = ch["name"]
                    if ch.get("sleeping", False):
                        state.force_check.discard(n)
                        continue
                    if n in busy:
                        continue
                    due = state.next_check_due.get(n, 0.0)
                    if n in forced or due <= cycle_now:
                        rec = state.recorders.get(n)
                        eligible.append((0 if n in forced else 1, 0 if rec and rec.is_recording else 1,
                                         0 if ch.get("single_live_mode") else 1, due, n.lower(), n,
                                         norm_platform(ch.get("platform"))))
                eligible.sort()
                use_helix = HELIX_ENABLED and time.time() >= _helix_backoff_until
                batch_running = any(k in _batch_futures for k in inflight)
                submitted: set[str] = set()
                twitch_names = [e[5] for e in eligible if e[6] == "twitch"] if use_helix else []
                if twitch_names and not batch_running:
                    fut = _batch_pool.submit(_helix_batch, twitch_names)
                    _batch_futures.add(fut)
                    inflight[fut] = twitch_names
                    submitted.update(twitch_names)
                singles_running = sum(1 for k in inflight if k not in _batch_futures)
                free = max(0, MONITOR_WORKERS - singles_running)
                for item in eligible:
                    n, platform = item[5], item[6]
                    if n in submitted or (use_helix and platform == "twitch"):
                        continue
                    if free <= 0:
                        break
                    fut = _pool.submit(check_streamlink, n, platform)
                    inflight[fut] = [n]
                    submitted.add(n)
                    free -= 1
                for n in submitted:
                    state.monitor.inflight[n] = cycle_now
                    state.next_check_due[n] = cycle_now + ERROR_CHECK_INTERVAL
                state.force_check.difference_update(submitted)
                state.monitor.heartbeat = time.time()
            for fut in [f for f in _batch_futures if f not in inflight]:
                _batch_futures.discard(fut)
            state.monitor_wake.wait(0.5)
            state.monitor_wake.clear()
        except Exception as exc:
            logger.error("Errore nel ciclo monitor: %s", exc, exc_info=True)
            state.monitor_wake.wait(1)


_batch_futures: set[Future] = set()


def _watchdog_loop() -> None:
    while True:
        time.sleep(30)
        try:
            thread = state.monitor.thread
            if thread is None or not thread.is_alive():
                logger.error("Monitor terminato — riavvio del thread")
                start_monitor()
            else:
                with state.lock:
                    stale = [time.time() - t for t in state.monitor.inflight.values()]
                if stale and max(stale) > 45:
                    logger.warning("Monitor degradato: controllo senza risposta da %.0fs", max(stale))
        except Exception as exc:
            logger.error("Errore watchdog: %s", exc)


def start_monitor() -> None:
    state.monitor.thread = threading.Thread(target=_monitor_loop, daemon=True, name="monitor")
    state.monitor.thread.start()
    state.monitor_wake.set()


def start() -> None:
    start_monitor()
    threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog").start()
    if HELIX_AVAILABLE:
        threading.Thread(target=_profiles_loop, daemon=True, name="twitch-profiles").start()


def shutdown() -> None:
    _pool.shutdown(wait=False)
    _batch_pool.shutdown(wait=False)
