"""Operazioni sui canali, condivise da API JSON, form e azioni multiple."""

import threading

from . import rerun, state
from .config import PLATFORMS, QUALITY_OPTIONS, STREAM_QUALITY, logger
from .monitor import clear_single_live_state
from .recorder import Recorder
from .util import norm_platform


class ActionError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.msg = msg
        self.status = status


def _channel(name: str) -> dict:
    ch = state.find_channel(name)
    if ch is None:
        raise ActionError("Canale non trovato.", 404)
    return ch


def _queue_check(name: str) -> None:
    state.next_check_due[name] = 0.0
    state.force_check.add(name)


def _done() -> None:
    state.mark_dirty()
    state.monitor_wake.set()
    state.request_broadcast()


def add_channel(name: str, platform: str, quality: str, extra: dict | None = None) -> str:
    platform = norm_platform(platform)
    if not name:
        raise ActionError("Nome canale non valido.")
    if quality not in QUALITY_OPTIONS:
        quality = STREAM_QUALITY
    with state.lock:
        if state.find_channel(name):
            raise ActionError("Canale già presente (nome unico tra le piattaforme).", 409)
        entry = {"name": name, "is_recording": True, "sleeping": False, "quality": quality,
                 "platform": platform, "rerun_policy": None}
        if extra:
            entry.update(extra)
        state.normalize_channel(entry)
        state.channels.append(entry)
        state.recorders[name] = Recorder(name, entry["quality"], platform)
        state.online_status[name] = False
        if not entry["sleeping"]:
            _queue_check(name)
    state.profiles_wake.set()
    _done()
    return f"{name} aggiunto ({PLATFORMS[platform]['label']})."


def set_recording(name: str, enabled: bool) -> str:
    rec_to_stop = None
    with state.lock:
        ch = _channel(name)
        clear_single_live_state(ch)
        state.offline_stop_due.pop(name, None)
        if enabled:
            if ch.get("sleeping"):
                raise ActionError("Il canale dorme: prima risveglialo.")
            ch["is_recording"] = True
        else:
            ch["is_recording"] = False
            rec = state.recorders.get(name)
            if rec and rec.is_recording:
                rec_to_stop = rec
        if not ch.get("sleeping"):
            _queue_check(name)
    if rec_to_stop:
        rec_to_stop.stop()
    _done()
    return f"Registrazione {'attiva' if enabled else 'in pausa'} per {name}."


def set_sleep(name: str, sleeping: bool) -> str:
    rec_to_stop = None
    with state.lock:
        ch = _channel(name)
        if not sleeping:
            ch["sleeping"] = False
            ch["is_recording"] = False
            _queue_check(name)
            msg = f"{name} risvegliato: monitor riattivo, registrazione in pausa. Premi Registra per riprendere."
        else:
            ch["sleeping"] = True
            ch["is_recording"] = False
            clear_single_live_state(ch)
            state.next_check_due[name] = float("inf")
            state.force_check.discard(name)
            state.offline_candidates.pop(name, None)
            state.offline_stop_due.pop(name, None)
            rec_to_stop = state.recorders.get(name)
            msg = f"{name} a dormire: registrazione ferma e controlli sospesi."
    if rec_to_stop:
        rec_to_stop.stop()
    _done()
    return msg


def toggle_sleep(name: str) -> tuple[str, bool]:
    with state.lock:
        sleeping = not _channel(name).get("sleeping", False)
    return set_sleep(name, sleeping), sleeping


def toggle_single_live(name: str) -> str:
    with state.lock:
        ch = _channel(name)
        if ch.get("single_live_mode"):
            clear_single_live_state(ch)
            msg = f"Modalità Una volta disattivata per {name}."
        else:
            ch["sleeping"] = False
            ch["single_live_mode"] = True
            ch["single_live_seen_online"] = bool(state.online_status.get(name, False))
            ch["single_live_offline_since"] = None
            ch["is_recording"] = True
            _queue_check(name)
            msg = (f"Una volta attiva per {name}: dopo la fine della live attendo 2 ore "
                   "una nuova diretta, poi metto il canale in pausa.")
    _done()
    return msg


def remove_channel(name: str) -> str:
    with state.lock:
        _channel(name)
        rec = state.recorders.pop(name, None)
        state.forget_channel(name)
    if rec:
        rec.stop()
    rerun.forget(name)
    _done()
    return f"{name} rimosso."


def change_quality(name: str, quality: str) -> tuple[str, bool]:
    """Cambia qualità. Se REC è attiva, il file corrente viene chiuso e ne parte uno nuovo."""
    if quality not in QUALITY_OPTIONS:
        raise ActionError("Qualità non valida.")
    with state.lock:
        ch = _channel(name)
        if ch.get("quality", STREAM_QUALITY) == quality:
            return "La qualità selezionata è già attiva.", False
        ch["quality"] = quality
        rec = state.recorders.get(name)
        platform = norm_platform(ch.get("platform"))
        was_recording = bool(rec and rec.is_recording)
        if rec and not was_recording:
            rec.quality = quality
        quality_lock = state.quality_change_locks.setdefault(name, threading.Lock())
    state.mark_dirty()
    if not was_recording:
        with state.lock:
            if ch.get("is_recording") and not ch.get("sleeping"):
                _queue_check(name)
        _done()
        return f"Qualità impostata su {quality}.", False

    def rollover():
        with quality_lock:
            rec.stop()
            thread = rec._thread
            if thread and thread is not threading.current_thread():
                thread.join(timeout=60)
            with state.lock:
                current = state.find_channel(name)
                if current is None or state.recorders.get(name) is not rec:
                    return
                state.recorders[name] = Recorder(name, current.get("quality", quality),
                                                 norm_platform(current.get("platform", platform)))
                if current.get("is_recording") and not current.get("sleeping"):
                    _queue_check(name)
            logger.info("Cambio qualità %s → %s: nuova sessione", name, quality)
            _done()

    threading.Thread(target=rollover, daemon=True, name=f"quality-{name}").start()
    state.request_broadcast()
    return (f"Qualità impostata su {quality}: chiudo il file attuale e ne avvio uno nuovo.", True)


def set_rerun_policy(name: str, policy: str | None) -> str:
    if policy not in ("record", "mark", "skip", None, ""):
        raise ActionError("Politica rerun non valida.")
    with state.lock:
        ch = _channel(name)
        ch["rerun_policy"] = policy or None
        _queue_check(name)
    _done()
    labels = {"record": "ignora i sospetti", "mark": "registra e segna", "skip": "salta le rerun"}
    return f"Rerun per {name}: {labels.get(policy, 'impostazione predefinita')}."


def rerun_override(name: str, verdict: str | None) -> str:
    with state.lock:
        _channel(name)
    result = rerun.set_override(name, verdict)
    if result is None:
        raise ActionError("Nessuna live registrata per questo canale.")
    with state.lock:
        if name in state.rerun_info:
            state.rerun_info[name] = result
        _queue_check(name)
    rec = state.recorders.get(name)
    if rec:
        rec.refresh_meta()
    _done()
    return {"rerun": f"Live di {name} segnata come rerun.",
            "original": f"Live di {name} segnata come originale: titolo ignorato in futuro."}.get(
        verdict, f"Valutazione automatica ripristinata per {name}.")


def pause_all() -> None:
    to_stop = []
    with state.lock:
        for ch in state.channels:
            clear_single_live_state(ch)
            ch["is_recording"] = False
            rec = state.recorders.get(ch["name"])
            if rec and rec.is_recording:
                to_stop.append(rec)
    for rec in to_stop:
        rec.stop()
    _done()


def resume_all() -> None:
    with state.lock:
        for ch in state.channels:
            if ch.get("sleeping"):
                continue
            clear_single_live_state(ch)
            ch["is_recording"] = True
            _queue_check(ch["name"])
    _done()


def force_check(name: str | None = None) -> tuple[int, bool]:
    skipped_sleeping = False
    with state.lock:
        if name:
            target = state.find_channel(name)
            if target and target.get("sleeping"):
                skipped_sleeping = True
            elif target:
                state.force_check.add(name)
        else:
            state.force_check.update(c["name"] for c in state.channels if not c.get("sleeping"))
        queued = len(state.force_check)
    state.monitor_wake.set()
    return queued, skipped_sleeping


BULK_OPS = ("pause", "resume", "sleep", "wake", "quality", "remove", "check", "rerun_policy")


def bulk(names: list[str], op: str, value=None) -> dict:
    if op not in BULK_OPS:
        raise ActionError("Operazione non valida.")
    done, failed = 0, []
    for name in names:
        try:
            if op == "pause":
                set_recording(name, False)
            elif op == "resume":
                set_recording(name, True)
            elif op == "sleep":
                set_sleep(name, True)
            elif op == "wake":
                set_sleep(name, False)
            elif op == "quality":
                change_quality(name, str(value))
            elif op == "remove":
                remove_channel(name)
            elif op == "check":
                force_check(name)
            elif op == "rerun_policy":
                set_rerun_policy(name, value or None)
            done += 1
        except ActionError as exc:
            failed.append({"channel": name, "msg": exc.msg})
    return {"ok": not failed, "done": done, "failed": failed}
