"""Scelta della qualità senza sondare prima la piattaforma.

Streamlink accetta una lista di varianti separate da virgola e apre la prima disponibile,
quindi la stessa logica di fallback di prima (stessa altezza con FPS alternativi, poi la
risoluzione inferiore più vicina, infine la superiore) diventa una sola invocazione.
"""

import re

_HEIGHTS = [2160, 1440, 1080, 936, 900, 864, 720, 540, 480, 360, 240, 160, 144]
_ALIASES = ("best", "worst", "audio_only")


def parse_variant(value: str) -> tuple[int, int | None] | None:
    match = re.match(r"^\s*(\d{3,4})p(\d{1,3})?\s*$", str(value), re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)) if match.group(2) else None


def _variants_for_height(height: int, fps: int | None) -> list[str]:
    if fps is None:
        order = [None, 30, 60, 50, 48]
    else:
        others = sorted([None, 30, 60, 50, 48], key=lambda f: (abs((f or 30) - fps), 0 if f is None else 1))
        order = [fps] + [f for f in others if f != fps]
    out = []
    for f in order:
        name = f"{height}p" if f is None else f"{height}p{f}"
        if name not in out:
            out.append(name)
    return out


def fallback_list(preferred: str) -> str:
    """Es. "720p60" -> "720p60,720p,720p30,…,480p,…,1080p60,…,best"."""
    preferred = (preferred or "best").strip()
    if preferred in _ALIASES:
        return preferred
    parsed = parse_variant(preferred)
    if not parsed:
        return f"{preferred},best"
    height, fps = parsed
    heights = sorted(set(_HEIGHTS) | {height}, reverse=True)
    lower = [h for h in heights if h < height]
    higher = sorted(h for h in heights if h > height)
    names = _variants_for_height(height, fps)
    for h in lower + higher:
        names += _variants_for_height(h, fps)
    seen, ordered = set(), []
    for name in [preferred] + names + ["best"]:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ",".join(ordered)


_OPENING_RE = re.compile(r"Opening stream:\s*([^\s(]+)", re.IGNORECASE)


def parse_opened_quality(log_line: str) -> str | None:
    """Estrae la variante scelta da una riga di log di Streamlink."""
    match = _OPENING_RE.search(log_line or "")
    return match.group(1) if match else None
