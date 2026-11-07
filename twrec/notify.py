"""Notifiche Telegram, Discord e webhook generico."""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import state
from .config import (
    NOTIFY_EVENTS, NOTIFY_TYPE, NOTIFY_URL, TELEGRAM_CHAT, TELEGRAM_PHOTO,
    TELEGRAM_TOKEN, logger,
)
from .util import norm_platform, platform_url


def send(event: str, channel: str, message: str) -> None:
    if event not in NOTIFY_EVENTS or not NOTIFY_TYPE:
        return
    threading.Thread(target=_do_notify, args=(event, channel, message), daemon=True,
                     name=f"notify-{event}").start()


def _post_json(url: str, payload: dict, timeout: int = 10) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=timeout)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _do_notify(event: str, channel: str, message: str) -> None:
    try:
        if NOTIFY_TYPE == "discord" and NOTIFY_URL:
            _post_json(NOTIFY_URL, {
                "username": "Twitch-Recorder",
                "embeds": [{"title": f"\U0001f4f9 {channel}", "description": message, "color": 0x9146FF,
                            "timestamp": _now_iso(), "footer": {"text": "Twitch-Recorder"}}],
            })
        elif NOTIFY_TYPE == "telegram" and TELEGRAM_TOKEN and TELEGRAM_CHAT:
            _notify_telegram(event, channel, message)
        elif NOTIFY_TYPE == "webhook" and NOTIFY_URL:
            _post_json(NOTIFY_URL, {"event": event, "channel": channel, "message": message,
                                    "timestamp": _now_iso(), "source": "twitch-recorder"})
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        logger.error("❌ Notifica %s/%s — HTTP %d: %s | Body: %s", NOTIFY_TYPE, channel, exc.code, exc.reason, body)
    except urllib.error.URLError as exc:
        logger.error("❌ Notifica %s/%s — Errore connessione: %s", NOTIFY_TYPE, channel, exc.reason)
    except Exception as exc:
        logger.error("❌ Notifica %s/%s — Errore: %s", NOTIFY_TYPE, channel, exc)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _notify_telegram(event: str, channel: str, message: str) -> None:
    ch = state.find_channel(channel)
    platform = norm_platform(ch.get("platform") if ch else None)
    base_api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    link = f'<a href="{platform_url(platform, channel)}">{_esc(channel)}</a>'
    if event == "online":
        caption = f"{link} — 🟢 Online"
        title = (state.live_meta.get(channel) or {}).get("title")
        if title:
            caption += f"\n{_esc(title)}"
        if TELEGRAM_PHOTO and platform == "twitch":
            try:
                _post_json(f"{base_api}/sendPhoto", {
                    "chat_id": TELEGRAM_CHAT, "caption": caption, "parse_mode": "HTML",
                    "photo": f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{channel}-640x360.jpg",
                }, timeout=15)
                return
            except Exception as exc:
                logger.warning("⚠ Telegram sendPhoto fallito per %s — %s — fallback a testo", channel, exc)
        text = caption
    elif event == "offline":
        text = f"{link} — 🔴 Offline"
    else:
        text = f"{link}\n{_esc(message)}"
    _post_json(f"{base_api}/sendMessage", {"chat_id": TELEGRAM_CHAT, "text": text, "parse_mode": "HTML",
                                           "disable_web_page_preview": True}, timeout=15)
