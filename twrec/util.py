"""Funzioni di supporto senza stato."""

import json
import os
import re
from datetime import datetime, timezone

from .config import DEFAULT_PLATFORM, PLATFORMS, logger


def norm_platform(platform: str | None) -> str:
    p = (platform or "").strip().lower()
    return p if p in PLATFORMS else DEFAULT_PLATFORM


def platform_url(platform: str, name: str) -> str:
    return PLATFORMS[norm_platform(platform)]["url"].format(name=name)


def clean_name(platform: str, raw: str) -> str:
    """Estrae e ripulisce il nome canale (anche se viene incollato un URL)."""
    raw = (raw or "").strip()
    for host in ("twitch.tv/", "kick.com/", "youtube.com/"):
        if host in raw:
            raw = raw.split(host)[-1]
            break
    raw = raw.split("?")[0].split("#")[0]
    if "/" in raw:
        segs = [s for s in raw.rstrip("/").split("/") if s and s != "live"]
        if segs:
            raw = segs[-1]
    raw = raw.lstrip("@")
    name = re.sub(r"[^A-Za-z0-9_.-]", "", raw)
    if norm_platform(platform) in ("twitch", "kick"):
        name = name.lower()
    return name


def as_bool(value, default=False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def coerce_timestamp(value):
    """Converte timestamp Unix o ISO-8601 in secondi Unix; i valori senza fuso sono UTC."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return None


def iso_timestamp(value):
    timestamp = coerce_timestamp(value)
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def human_size(nbytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Impossibile leggere %s: %s", path, exc)
        return default


def write_json_atomic(path: str, data, indent: int | None = 2) -> bool:
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=indent, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.error("Impossibile salvare %s: %s", path, exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def normalize_title(title: str | None) -> str:
    """Titolo normalizzato per i confronti: minuscolo, senza emoji, comandi chat e punteggiatura."""
    text = str(title or "").casefold()
    text = re.sub(r"![a-z0-9_]+", " ", text)          # !discord, !prime …
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[@#][\w]+", " ", text)
    text = re.sub(r"\d{1,2}[/.-]\d{1,2}([/.-]\d{2,4})?", " ", text)  # date variabili
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()
