"""Verifica dello stato online dei canali.

* Twitch con credenziali Helix: un'unica richiesta HTTP per fino a 100 canali.
* Altrimenti (o per Kick/YouTube): un processo `streamlink --json` per canale.
"""

import json
import subprocess
import threading
from dataclasses import dataclass, field

from . import twitch_api
from .config import SKIP_TWITCH_ADS, logger
from .util import norm_platform, platform_url


@dataclass
class CheckResult:
    online: bool
    certain: bool
    kind: str                     # live | offline | private | error
    error: str | None = None
    meta: dict = field(default_factory=dict)
    tagged_rerun: bool = False
    via: str = "streamlink"


PRIVATE_PHRASES = (
    "this stream is private", "this channel is private", "private stream",
    "stream is private", "subscriber-only", "subscribers-only",
    "subscriber only", "subscribers only", "sub-only stream",
    "only available to subscribers",
)
OFFLINE_PHRASES = (
    "no playable streams found", "no streams found", "is offline",
    "currently offline", "not live", "stream is offline", "no streams available",
)


def meta_from_helix(stream: dict) -> dict:
    return {
        "title": stream.get("title") or "",
        "game": stream.get("game_name") or "",
        "viewers": stream.get("viewer_count"),
        "started_at": stream.get("started_at"),
        "user_id": stream.get("user_id"),
        "stream_id": stream.get("id"),
        "display_name": stream.get("user_name"),
        "tags": stream.get("tags") or [],
        "language": stream.get("language"),
        "source": "helix",
    }


def check_streamlink(channel_name: str, platform: str) -> CheckResult:
    platform = norm_platform(platform)
    try:
        result = subprocess.run(
            ["streamlink", "--json", platform_url(platform, channel_name)],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        logger.warning("⚠ %s — timeout controllo online", channel_name)
        return CheckResult(False, False, "error", "Timeout durante il controllo della diretta")
    except Exception as exc:
        logger.warning("⚠ %s — errore controllo online: %s", channel_name, exc)
        return CheckResult(False, False, "error", str(exc)[:300])

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    combined = f"{stdout} {stderr}".strip()
    parsed = None
    if stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None
    lowered = combined.lower()
    if any(phrase in lowered for phrase in PRIVATE_PHRASES):
        return CheckResult(False, True, "private")

    meta = {}
    if isinstance(parsed, dict) and isinstance(parsed.get("metadata"), dict):
        md = parsed["metadata"]
        meta = {"title": md.get("title") or "", "game": md.get("category") or "",
                "display_name": md.get("author"), "source": "streamlink"}

    streams = parsed.get("streams") if isinstance(parsed, dict) else None
    if isinstance(streams, (dict, list)):
        if streams:
            tagged = platform == "twitch" and twitch_api.is_tagged_rerun({"title": meta.get("title")})
            return CheckResult(True, True, "live", meta=meta, tagged_rerun=tagged)
        return CheckResult(False, True, "offline")
    if any(phrase in lowered for phrase in OFFLINE_PHRASES):
        return CheckResult(False, True, "offline")
    if isinstance(parsed, dict) and parsed.get("error"):
        err = str(parsed.get("error"))
        if any(phrase in err.lower() for phrase in OFFLINE_PHRASES):
            return CheckResult(False, True, "offline")
        return CheckResult(False, False, "error", err[:300])
    if result.returncode == 0:
        return CheckResult(False, False, "error", (combined or "Risposta Streamlink senza stato riconoscibile")[:300])
    return CheckResult(False, False, "error", (combined or f"Streamlink ha restituito codice {result.returncode}")[:300])


def check_twitch_batch(logins: list[str]) -> dict[str, CheckResult]:
    """Controlla molti canali Twitch con Helix. Solleva eccezione se Helix non risponde."""
    live = twitch_api.get_streams(logins)
    out: dict[str, CheckResult] = {}
    for login in logins:
        stream = live.get(login.lower())
        if stream:
            out[login] = CheckResult(True, True, "live", meta=meta_from_helix(stream),
                                     tagged_rerun=twitch_api.is_tagged_rerun(stream), via="helix")
        else:
            out[login] = CheckResult(False, True, "offline", via="helix")
    return out


def check_channel(channel_name: str, platform: str) -> CheckResult:
    """Controllo singolo: Helix per Twitch quando configurato, altrimenti Streamlink."""
    from .config import HELIX_ENABLED
    if norm_platform(platform) == "twitch" and HELIX_ENABLED:
        try:
            return check_twitch_batch([channel_name])[channel_name]
        except Exception as exc:
            logger.warning("Helix non disponibile (%s): uso Streamlink per %s", exc, channel_name)
    return check_streamlink(channel_name, platform)


# ── Comando base Streamlink ──

_legacy_ads_lock = threading.Lock()
_legacy_ads_supported: bool | None = None


def streamlink_base_command(platform: str) -> list[str]:
    """Aggiunge --twitch-disable-ads solo alle versioni di Streamlink che lo supportano ancora."""
    global _legacy_ads_supported
    cmd = ["streamlink", "--loglevel", "info"]
    if norm_platform(platform) == "twitch" and SKIP_TWITCH_ADS:
        with _legacy_ads_lock:
            if _legacy_ads_supported is None:
                try:
                    probe = subprocess.run(["streamlink", "--twitch-disable-ads", "--version"],
                                           capture_output=True, text=True, timeout=8)
                    _legacy_ads_supported = probe.returncode == 0
                except Exception:
                    _legacy_ads_supported = False
            if _legacy_ads_supported:
                cmd.append("--twitch-disable-ads")
    return cmd
