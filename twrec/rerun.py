"""Rilevamento delle rerun, anche quando non sono dichiarate.

Twitch espone solo il tag ufficiale «Rerun», che molti non usano. Qui ogni live riceve
un punteggio 0-100 costruito da segnali indipendenti e leggeri (nessuna analisi audio/video):

* rerun dichiarata (tag, tipo stream, prefisso del titolo) → confermata
* parole chiave nel titolo (replica, rerun, rewatch, differita…)
* titolo identico/quasi identico a una live passata o a una VOD di almeno 3 ore prima,
  ignorando i titoli «fissi» che il canale riusa spesso
* stessa categoria della live corrispondente
* spettatori molto sotto la mediana del canale (solo con Helix)
* orario di inizio insolito rispetto allo storico del canale

L'utente può correggere la valutazione di una sessione; un «non è una rerun» mette il
titolo fra quelli da ignorare per quel canale.
"""

import re
import statistics
import threading
import time
from difflib import SequenceMatcher

from . import twitch_api
from .config import (
    HELIX_AVAILABLE, HISTORY_FILE, RERUN_DEFAULT_POLICY, RERUN_PROBABLE_SCORE,
    RERUN_SUSPECT_SCORE, SKIP_RERUNS, logger,
)
from .util import coerce_timestamp, normalize_title, read_json, write_json_atomic

MAX_SESSIONS = 80
MIN_GAP_SECONDS = 3 * 3600        # le live più vicine di 3 h non contano come «originale»
REASSESS_SECONDS = 10 * 60

_KEYWORDS_STRONG = re.compile(
    r"\b(re-?run|replica|rewatch|vodcast|rebroadcast|re-?upload|repeat|differita|"
    r"registrat[ao]|non in diretta|not live|wiederholung|rediffusion|repetici[oó]n)\b",
    re.IGNORECASE,
)
_KEYWORDS_WEAK = re.compile(r"\b(vods?|throwback|best of|highlights?)\b", re.IGNORECASE)

_lock = threading.RLock()
_history: dict[str, dict] = {}
_loaded = False


def _load() -> None:
    global _loaded
    if not _loaded:
        data = read_json(HISTORY_FILE, {})
        if isinstance(data, dict):
            _history.update({k: v for k, v in data.items() if isinstance(v, dict)})
        _loaded = True


def _save() -> None:
    write_json_atomic(HISTORY_FILE, _history, indent=None)


def _channel(name: str) -> dict:
    _load()
    entry = _history.setdefault(name, {})
    entry.setdefault("sessions", [])
    entry.setdefault("ignore_titles", [])
    return entry


def effective_policy(channel: dict) -> str:
    policy = channel.get("rerun_policy")
    if policy in ("record", "mark", "skip"):
        return policy
    return RERUN_DEFAULT_POLICY


def _open_session(entry: dict) -> dict | None:
    sessions = entry["sessions"]
    if sessions and sessions[-1].get("ended_at") is None:
        return sessions[-1]
    return None


def on_live(name: str, meta: dict, tagged: bool, now: float | None = None) -> dict:
    """Aggiorna la sessione corrente e ritorna la valutazione rerun."""
    now = now or time.time()
    with _lock:
        entry = _channel(name)
        session = _open_session(entry)
        stream_id = meta.get("stream_id")
        started = coerce_timestamp(meta.get("started_at")) or now
        if session and stream_id and session.get("stream_id") and session["stream_id"] != stream_id:
            session["ended_at"] = now
            session = None
        changed = False
        if session is None:
            session = {
                "id": f"{int(started)}-{stream_id or 'x'}",
                "stream_id": stream_id, "started_at": started, "ended_at": None,
                "titles": [], "game": "", "peak_viewers": 0, "override": None,
            }
            entry["sessions"].append(session)
            del entry["sessions"][:-MAX_SESSIONS]
            changed = True
        title = (meta.get("title") or "").strip()
        if title and title not in session["titles"]:
            session["titles"].append(title)
            del session["titles"][:-10]
            changed = True
        if meta.get("game") and session.get("game") != meta["game"]:
            session["game"] = meta["game"]
            changed = True
        viewers = meta.get("viewers")
        if isinstance(viewers, int) and viewers > session.get("peak_viewers", 0):
            session["peak_viewers"] = viewers
        stale = now - session.get("assessed_at", 0) > REASSESS_SECONDS
        if changed or stale or "assessment" not in session:
            session["assessment"] = _assess(entry, session, meta, tagged, now)
            session["assessed_at"] = now
            _save()
        result = dict(session["assessment"])
        result["session_id"] = session["id"]
        result["override"] = session.get("override")
        return _apply_override(result)


def on_offline(name: str, now: float | None = None) -> None:
    with _lock:
        entry = _channel(name)
        session = _open_session(entry)
        if session:
            session["ended_at"] = now or time.time()
            _save()


def set_override(name: str, verdict: str | None) -> dict | None:
    """verdict: "rerun", "original" oppure None per tornare alla valutazione automatica."""
    with _lock:
        entry = _channel(name)
        session = _open_session(entry) or (entry["sessions"][-1] if entry["sessions"] else None)
        if not session:
            return None
        session["override"] = verdict if verdict in ("rerun", "original") else None
        if verdict == "original":
            for title in session.get("titles", []):
                norm = normalize_title(title)
                if norm and norm not in entry["ignore_titles"]:
                    entry["ignore_titles"].append(norm)
            del entry["ignore_titles"][:-50]
        _save()
        result = dict(session.get("assessment") or {"score": 0, "level": "none", "reasons": []})
        result["session_id"] = session["id"]
        result["override"] = session["override"]
        return _apply_override(result)


def forget(name: str) -> None:
    with _lock:
        _load()
        if _history.pop(name, None) is not None:
            _save()


def _apply_override(result: dict) -> dict:
    if result.get("override") == "rerun":
        result["level"] = "confirmed"
        result["score"] = 100
    elif result.get("override") == "original":
        result["level"] = "none"
    return result


def level_for(score: int, confirmed: bool) -> str:
    if confirmed:
        return "confirmed"
    if score >= RERUN_PROBABLE_SCORE:
        return "probable"
    if score >= RERUN_SUSPECT_SCORE:
        return "suspect"
    return "none"


def _circular_hour_distance(a: float, b: float) -> float:
    d = abs(a - b) % 24
    return min(d, 24 - d)


def _assess(entry: dict, session: dict, meta: dict, tagged: bool, now: float) -> dict:
    reasons: list[dict] = []

    def add(points: int, text: str):
        reasons.append({"points": points, "text": text})

    if tagged:
        add(100, "Rerun dichiarata da Twitch (tag o titolo)")
        return {"score": 100, "level": "confirmed", "reasons": reasons, "matched": None}

    title = session["titles"][-1] if session["titles"] else (meta.get("title") or "")
    norm = normalize_title(title)
    started = session["started_at"]

    if title and _KEYWORDS_STRONG.search(title):
        add(45, f"Il titolo contiene «{_KEYWORDS_STRONG.search(title).group(0)}»")
    elif title and _KEYWORDS_WEAK.search(title):
        add(20, f"Il titolo contiene «{_KEYWORDS_WEAK.search(title).group(0)}»")

    past = [s for s in entry["sessions"]
            if s is not session and s.get("started_at")
            and started - (s.get("ended_at") or s["started_at"]) >= MIN_GAP_SECONDS
            and s.get("override") != "rerun"]
    candidates: list[tuple[str, float, str]] = []   # (titolo normalizzato, inizio, categoria)
    for s in past:
        for t in s.get("titles", []):
            candidates.append((normalize_title(t), s["started_at"], s.get("game", "")))
    if HELIX_AVAILABLE and meta.get("user_id"):
        for video in twitch_api.get_archive_videos(meta["user_id"]):
            created = coerce_timestamp(video.get("created_at"))
            if created and started - created >= MIN_GAP_SECONDS:
                candidates.append((normalize_title(video.get("title")), created, ""))

    matched = None
    if norm and len(norm) >= 10 and norm not in entry.get("ignore_titles", []):
        sessions_with_title = sum(1 for s in past if norm in {normalize_title(t) for t in s.get("titles", [])})
        recurring = sessions_with_title >= 3 or (len(past) >= 5 and sessions_with_title / len(past) >= 0.3)
        if recurring:
            add(0, "Titolo ricorrente del canale: non considerato")
        else:
            best_ratio, best = 0.0, None
            for cand_title, cand_start, cand_game in candidates:
                if not cand_title:
                    continue
                ratio = 1.0 if cand_title == norm else SequenceMatcher(None, norm, cand_title).ratio()
                if ratio > best_ratio:
                    best_ratio, best = ratio, (cand_title, cand_start, cand_game)
            if best and best_ratio >= 0.92:
                when = time.strftime("%d/%m/%Y", time.localtime(best[1]))
                if best_ratio == 1.0:
                    add(45, f"Titolo identico a una live del {when}")
                else:
                    add(30, f"Titolo quasi identico ({int(best_ratio * 100)}%) a una live del {when}")
                matched = {"started_at": best[1], "ratio": round(best_ratio, 2)}
                game = session.get("game") or meta.get("game")
                if game and best[2] and best[2] == game:
                    add(10, f"Stessa categoria ({game})")

    peaks = [s.get("peak_viewers", 0) for s in past if s.get("peak_viewers")]
    viewers = session.get("peak_viewers", 0)
    if len(peaks) >= 5 and viewers and now - started >= 15 * 60:
        median = statistics.median(peaks)
        if median >= 20:
            if viewers < 0.25 * median:
                add(20, f"Spettatori molto bassi ({viewers} contro una mediana di {int(median)})")
            elif viewers < 0.4 * median:
                add(10, f"Spettatori bassi ({viewers} contro una mediana di {int(median)})")

    hours = [time.localtime(s["started_at"]).tm_hour + time.localtime(s["started_at"]).tm_min / 60 for s in past]
    if len(hours) >= 8:
        current_hour = time.localtime(started).tm_hour + time.localtime(started).tm_min / 60
        near = sum(1 for h in hours if _circular_hour_distance(h, current_hour) <= 1.5)
        if near == 0:
            add(15, f"Orario d'inizio insolito ({time.strftime('%H:%M', time.localtime(started))})")

    score = min(99, sum(r["points"] for r in reasons))
    return {"score": score, "level": level_for(score, False), "reasons": reasons, "matched": matched}


def decide(channel: dict, assessment: dict | None) -> str:
    """Ritorna l'azione: "skip" (non registrare), "mark" (registra e segna) o "record"."""
    if not assessment:
        return "record"
    level = assessment.get("level", "none")
    policy = effective_policy(channel)
    if level == "confirmed":
        if SKIP_RERUNS or policy == "skip":
            return "skip"
        return "mark"
    if level == "probable" and policy == "skip":
        return "skip"
    if level in ("probable", "suspect") and policy != "record":
        return "mark"
    return "record"


def stats(name: str) -> dict:
    with _lock:
        entry = _channel(name)
        return {"sessions": len(entry["sessions"]), "ignored_titles": len(entry["ignore_titles"])}


logger.debug("Rilevamento rerun: soglie sospetta=%d probabile=%d", RERUN_SUSPECT_SCORE, RERUN_PROBABLE_SCORE)
