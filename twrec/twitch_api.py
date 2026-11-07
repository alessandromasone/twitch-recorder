"""Client minimale per Twitch Helix (token app, stream in blocco, utenti, VOD)."""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import HELIX_AVAILABLE, TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, logger

_token_lock = threading.Lock()
_token = ""
_token_expires = 0.0

_users_lock = threading.Lock()
_users: dict[str, tuple[float, dict]] = {}      # login -> (expires, user)
_videos_lock = threading.Lock()
_videos: dict[str, tuple[float, list]] = {}     # user_id -> (expires, videos)

USER_TTL = 24 * 3600
VIDEOS_TTL = 3 * 3600


class HelixError(RuntimeError):
    pass


def _get_token(force: bool = False) -> str:
    global _token, _token_expires
    if not HELIX_AVAILABLE:
        raise HelixError("mancano TWITCH_CLIENT_ID e/o TWITCH_CLIENT_SECRET")
    with _token_lock:
        if not force and _token and _token_expires > time.time() + 30:
            return _token
        payload = urllib.parse.urlencode({
            "client_id": TWITCH_CLIENT_ID,
            "client_secret": TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://id.twitch.tv/oauth2/token", data=payload, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise HelixError("Twitch non ha restituito un app access token valido")
        _token = token
        _token_expires = time.time() + max(60, int(data.get("expires_in", 3600))) - 30
        return token


def request(method: str, path: str, params: list[tuple[str, str]] | None = None, body: dict | None = None) -> dict:
    """Chiamata Helix autenticata con token app; rinnova il token una volta se scaduto."""
    url = f"https://api.twitch.tv/helix/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    for attempt in (0, 1):
        token = _get_token(force=attempt == 1)
        headers = {"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                raw = response.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0:
                continue  # token revocato o scaduto: rinnova e riprova una volta
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            raise HelixError(f"HTTP {exc.code} {exc.reason} {detail}".strip()) from exc
    raise HelixError("autenticazione Helix non riuscita")


def _get(path: str, params: list[tuple[str, str]]) -> dict:
    body = request("GET", path, params)
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise HelixError("risposta Helix senza campo data valido")
    return body


def get_streams(logins: list[str]) -> dict[str, dict]:
    """Ritorna {login: stream} per i canali live. Una richiesta ogni 100 canali."""
    result: dict[str, dict] = {}
    unique = sorted({l.strip().lower() for l in logins if l and l.strip()})
    for start in range(0, len(unique), 100):
        chunk = unique[start:start + 100]
        body = _get("streams", [("user_login", login) for login in chunk] + [("first", "100")])
        for row in body["data"]:
            if isinstance(row, dict):
                login = str(row.get("user_login", "")).lower()
                if login:
                    result[login] = row
    return result


def get_users(logins: list[str]) -> dict[str, dict]:
    """Profili (id, avatar, nome visualizzato) con cache di 24 h."""
    now = time.time()
    out: dict[str, dict] = {}
    missing = []
    with _users_lock:
        for login in {l.lower() for l in logins if l}:
            cached = _users.get(login)
            if cached and cached[0] > now:
                out[login] = cached[1]
            else:
                missing.append(login)
    for start in range(0, len(missing), 100):
        chunk = missing[start:start + 100]
        try:
            body = _get("users", [("login", login) for login in chunk])
        except Exception as exc:
            logger.warning("Icone dei canali non disponibili (Helix users): %s", exc)
            break
        with _users_lock:
            for row in body["data"]:
                login = str(row.get("login", "")).lower()
                if login:
                    _users[login] = (now + USER_TTL, row)
                    out[login] = row
            for login in chunk:
                if login not in out:
                    _users[login] = (now + 3600, {})   # canale inesistente: non ritentare subito
    return out


def get_archive_videos(user_id: str) -> list[dict]:
    """Ultime VOD (archivi delle dirette) del canale, con cache di 3 h."""
    if not user_id:
        return []
    now = time.time()
    with _videos_lock:
        cached = _videos.get(user_id)
        if cached and cached[0] > now:
            return cached[1]
    try:
        body = _get("videos", [("user_id", user_id), ("type", "archive"), ("first", "30")])
        videos = [v for v in body["data"] if isinstance(v, dict)]
    except Exception as exc:
        logger.debug("Helix videos non disponibile per %s: %s", user_id, exc)
        videos = []
    with _videos_lock:
        _videos[user_id] = (now + (VIDEOS_TTL if videos else 600), videos)
    return videos


def is_tagged_rerun(stream: dict | None) -> bool:
    """Rerun dichiarata: tag ufficiale, tipo stream o prefisso esplicito nel titolo."""
    import re
    if not isinstance(stream, dict):
        return False
    if str(stream.get("type", "")).lower() == "rerun":
        return True
    tags = stream.get("tags") or []
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict):
                tag = tag.get("name") or tag.get("tag_name") or ""
            if re.sub(r"[^a-z0-9]", "", str(tag).casefold()) == "rerun":
                return True
    title = str(stream.get("title") or "")
    return bool(re.match(r"^\s*(?:(?:\[|\()\s*)?re[\s-]?run\b", title, re.IGNORECASE))
