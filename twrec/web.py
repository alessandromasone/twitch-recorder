"""Applicazione Flask: autenticazione, pagine, API JSON, preview e miniature."""

import atexit
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import threading
import time
import urllib.parse
from collections import deque
from datetime import timedelta

from flask import (
    Flask, Response, abort, jsonify, redirect, render_template, request,
    send_file, send_from_directory, session, url_for,
)

from . import actions, monitor, preview, state, status, storage
from .actions import ActionError
from .config import (
    APP_PASSWORD, APP_SESSION_HOURS, CHANNELS_FILE, DATA_DIR, DEFAULT_PLATFORM, LOGIN_ENABLED,
    QUALITY_OPTIONS, RECORDINGS_DIR, SESSION_COOKIE_SECURE, STREAM_QUALITY, logger,
)
from .recorder import Recorder
from .util import as_bool, clean_name, coerce_timestamp, iso_timestamp, norm_platform

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"),
            static_folder=os.path.join(BASE_DIR, "static"))


def _load_persistent_secret_key() -> str:
    configured = os.getenv("SECRET_KEY", "").strip()
    if configured:
        return configured
    secret_path = os.path.join(DATA_DIR, ".twitch-recorder-session-secret")
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = None
        if fd is not None:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(secrets.token_urlsafe(48))
        with open(secret_path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
        if value:
            return value
    except OSError as exc:
        logger.warning("Impossibile salvare la chiave di sessione persistente: %s", exc)
    return secrets.token_urlsafe(48)


app.secret_key = _load_persistent_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=APP_SESSION_HOURS),
    SEND_FILE_MAX_AGE_DEFAULT=60 * 60 * 24 * 30,   # gli asset statici hanno ?v=hash
    JSON_SORT_KEYS=False,
)


def _static_version() -> str:
    digest = hashlib.sha1()
    for name in sorted(os.listdir(app.static_folder)):
        path = os.path.join(app.static_folder, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                digest.update(fh.read())
    return digest.hexdigest()[:10]


STATIC_VERSION = _static_version()

# ── Autenticazione ──

_login_attempts: dict[str, deque] = {}
_login_lock = threading.Lock()
_LOGIN_WINDOW = 300
_LOGIN_MAX = 5


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def _template_context():
    return {"csrf_token_value": _csrf_token(), "login_enabled": LOGIN_ENABLED,
            "session_hours": APP_SESSION_HOURS, "static_v": STATIC_VERSION}


def _login_wait(ip: str) -> int:
    now = time.time()
    with _login_lock:
        attempts = _login_attempts.setdefault(ip, deque())
        while attempts and now - attempts[0] >= _LOGIN_WINDOW:
            attempts.popleft()
        return max(0, int(_LOGIN_WINDOW - (now - attempts[0]))) if len(attempts) >= _LOGIN_MAX else 0


def _login_failed(ip: str) -> None:
    with _login_lock:
        _login_attempts.setdefault(ip, deque()).append(time.time())


def _safe_next(target: str | None) -> str:
    target = target or "/"
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


@app.before_request
def _require_login_and_csrf():
    if not LOGIN_ENABLED or request.endpoint in ("login", "static", "health"):
        return None
    if not session.get("authenticated"):
        if (request.path.startswith(("/api/", "/live/", "/recordings/", "/thumb/")) or request.is_json
                or request.headers.get("Accept", "").startswith("application/json")):
            return jsonify({"ok": False, "login_required": True, "error": "Autenticazione richiesta"}), 401
        next_path = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=next_path))
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        expected = session.get("_csrf_token", "")
        if not expected or not supplied or not hmac.compare_digest(str(supplied), str(expected)):
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"ok": False, "error": "Token di sicurezza non valido. Ricarica la pagina."}), 400
            abort(400)
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if not LOGIN_ENABLED or session.get("authenticated"):
        return redirect(url_for("index"))
    raw_next = request.form.get("next") if request.method == "POST" else request.args.get("next")
    next_path = _safe_next(raw_next)
    error = None
    if request.method == "POST":
        supplied = request.form.get("csrf_token", "")
        expected = session.get("_csrf_token", "")
        if not expected or not supplied or not hmac.compare_digest(supplied, expected):
            error = "Sessione di accesso scaduta. Ricarica la pagina e riprova."
        else:
            ip = request.remote_addr or "unknown"
            wait = _login_wait(ip)
            if wait:
                error = f"Troppi tentativi. Riprova tra {max(1, wait)} secondi."
            elif hmac.compare_digest(request.form.get("password", "").encode("utf-8"), APP_PASSWORD.encode("utf-8")):
                with _login_lock:
                    _login_attempts.pop(ip, None)
                session.clear()
                session["authenticated"] = True
                session.permanent = True
                session["_csrf_token"] = secrets.token_urlsafe(32)
                return redirect(next_path)
            else:
                _login_failed(ip)
                error = "Password non corretta."
    response = app.make_response(render_template("login.html", error=error, next_path=next_path))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login") if LOGIN_ENABLED else url_for("index"))


# ── Pagine ──

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # Compatibilità con i vecchi form HTML: stesse azioni dell'API JSON.
        action = request.form.get("action", "")
        platform = norm_platform(request.form.get("platform"))
        name = clean_name(platform, request.form.get("channel") or "")
        quality = request.form.get("quality", STREAM_QUALITY)
        try:
            _dispatch(action, name, platform, quality, request.form)
        except ActionError:
            pass
        return redirect(url_for("index"))
    response = app.make_response(render_template("index.html"))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/recordings/<path:filename>")
def download_recording(filename):
    path = storage.safe_recording_path(filename)
    if not path:
        abort(400)
    return send_from_directory(RECORDINGS_DIR, os.path.basename(path), conditional=True,
                               as_attachment=request.args.get("dl") == "1")


@app.route("/delete_recording", methods=["POST"])
def delete_recording():
    storage.delete_recording(request.form.get("filename", ""))
    return redirect(url_for("index"))


@app.route("/live/<platform>/<room>")
def preview_live(platform, room):
    try:
        source = preview.get_or_create(platform, room)
    except LookupError:
        abort(404)
    except Exception as exc:
        logger.warning("Impossibile avviare la preview di %s: %s", room, exc)
        return jsonify(error="Impossibile avviare il flusso live. Verifica che la diretta sia online."), 503
    return Response(preview.stream(source), mimetype="video/mp2t", headers={
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache", "X-Accel-Buffering": "no", "X-Content-Type-Options": "nosniff",
        "X-Preview-Source": "recorder-shared" if source.mode == "shared" else "temporary",
    })


@app.route("/thumb/live/<room>.jpg")
def thumb_live(room):
    image = preview.live_thumbnail(room)
    if not image:
        abort(404)
    return Response(image, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


@app.route("/thumb/rec/<path:filename>.jpg")
def thumb_recording(filename):
    path = storage.recording_thumb(filename)
    if not path:
        abort(404)
    return send_file(path, mimetype="image/jpeg", max_age=86400 * 7)


# ── API ──

@app.route("/api/status")
def api_status():
    return jsonify(status.build_full())


@app.route("/api/stream")
def api_stream():
    q, initial = status.subscribe()

    def events():
        try:
            yield "retry: 3000\n\n"
            yield initial
            while True:
                try:
                    msg = q.get(timeout=20)
                except Exception:
                    yield ": heartbeat\n\n"
                    continue
                if msg is None:
                    return
                yield msg
        except GeneratorExit:
            pass
        finally:
            status.unsubscribe(q)

    return Response(events(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive",
    })


EXPORT_FIELDS = ["name", "platform", "quality", "is_recording", "sleeping", "rerun_policy",
                 "single_live_mode", "single_live_seen_online", "single_live_offline_since",
                 "last_live", "last_online", "last_status", "last_checked", "last_status_change"]


@app.route("/api/export")
def api_export():
    fmt = request.args.get("format", "json").lower()
    with state.lock:
        data = [{
            "name": ch["name"], "platform": norm_platform(ch.get("platform")),
            "quality": ch.get("quality", STREAM_QUALITY), "is_recording": ch.get("is_recording", False),
            "sleeping": ch.get("sleeping", False), "rerun_policy": ch.get("rerun_policy") or "",
            "single_live_mode": bool(ch.get("single_live_mode")),
            "single_live_seen_online": bool(ch.get("single_live_seen_online")),
            "single_live_offline_since": iso_timestamp(ch.get("single_live_offline_since")),
            "last_live": iso_timestamp(ch.get("last_live") or ch.get("last_online")),
            "last_online": iso_timestamp(ch.get("last_online")),
            "last_status": ch.get("last_status", "unknown"),
            "last_checked": iso_timestamp(ch.get("last_checked")),
            "last_status_change": iso_timestamp(ch.get("last_status_change")),
        } for ch in state.channels]
    if fmt == "csv":
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=EXPORT_FIELDS)
        writer.writeheader()
        writer.writerows(data)
        return Response(out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=twitch-channels.csv"})
    return Response(json.dumps(data, indent=2), mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=twitch-channels.json"})


def _import_row(item: dict) -> dict:
    return {
        "name": item.get("name", ""),
        "platform": norm_platform(item.get("platform")),
        "quality": (item.get("quality") or STREAM_QUALITY),
        "_quality_provided": bool(str(item.get("quality") or "").strip()),
        "is_recording": as_bool(item.get("is_recording", True), True),
        "sleeping": as_bool(item.get("sleeping", False)),
        "rerun_policy": item.get("rerun_policy") or None,
        "single_live_mode": as_bool(item.get("single_live_mode", False)),
        "single_live_seen_online": as_bool(item.get("single_live_seen_online", False)),
        "single_live_offline_since": coerce_timestamp(item.get("single_live_offline_since")),
        "last_live": coerce_timestamp(item.get("last_live") or item.get("last_online")),
        "last_online": coerce_timestamp(item.get("last_online") or item.get("last_live")),
        "last_status": item.get("last_status", "unknown"),
        "last_checked": coerce_timestamp(item.get("last_checked")),
        "last_status_change": coerce_timestamp(item.get("last_status_change")),
    }


@app.route("/api/import", methods=["POST"])
def api_import():
    imported = updated = skipped = 0
    update_quality = as_bool(request.form.get("update_existing_quality",
                                              request.args.get("update_existing_quality")), False)
    try:
        file = request.files.get("file")
        content = file.read().decode("utf-8-sig") if file else request.get_data(as_text=True)
        stripped = content.strip()
        rows: list[dict] = []
        if stripped.startswith(("[", "{")):
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                parsed = [parsed]
            for item in parsed:
                if isinstance(item, str):
                    rows.append(_import_row({"name": item}))
                elif isinstance(item, dict) and item.get("name"):
                    rows.append(_import_row(item))
        else:
            for row in csv.DictReader(io.StringIO(content)):
                if (row.get("name") or "").strip():
                    rows.append(_import_row({k: v for k, v in row.items() if v not in (None, "")}))
        for row in rows:
            plat = row["platform"]
            name = clean_name(plat, str(row["name"]))
            if not name:
                skipped += 1
                continue
            if state.find_channel(name):
                if update_quality and row["_quality_provided"] and row["quality"] in QUALITY_OPTIONS:
                    _msg, _restart = actions.change_quality(name, row["quality"])
                    if _msg.startswith("La qualità selezionata è già attiva"):
                        skipped += 1
                    else:
                        updated += 1
                else:
                    skipped += 1
                continue
            extra = {k: v for k, v in row.items() if not k.startswith("_") and k not in ("name", "platform", "quality")}
            if extra.get("sleeping"):
                extra["is_recording"] = False
                extra["single_live_mode"] = False
            if not extra.get("single_live_mode"):
                extra["single_live_seen_online"] = False
                extra["single_live_offline_since"] = None
            extra.update({"last_check_result": "checking", "check_failures": 0, "last_error": ""})
            actions.add_channel(name, plat, row["quality"] if row["quality"] in QUALITY_OPTIONS else STREAM_QUALITY, extra)
            imported += 1
    except ActionError as exc:
        return jsonify({"error": exc.msg}), exc.status
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"imported": imported, "updated": updated, "skipped": skipped})


@app.route("/api/check", methods=["POST"])
def api_force_check():
    data = request.get_json(silent=True) or request.form
    room = (data.get("channel") or "").strip()
    queued, skipped = actions.force_check(room or None)
    return jsonify({"ok": True, "queued": queued, "skipped_sleeping": skipped})


def _dispatch(action: str, name: str, platform: str, quality: str, data) -> dict:
    if action == "add":
        return {"msg": actions.add_channel(name, platform, quality)}
    if action == "pause_all":
        actions.pause_all()
        return {"msg": "Tutti i canali in pausa."}
    if action == "resume_all":
        actions.resume_all()
        return {"msg": "Registrazione riattivata per tutti i canali svegli."}
    if action == "bulk":
        names = [str(n) for n in (data.get("channels") or []) if n]
        return actions.bulk(names, data.get("op", ""), data.get("value"))
    if not name:
        raise ActionError("Canale mancante.")
    if action in ("pause", "resume"):
        return {"msg": actions.set_recording(name, action == "resume")}
    if action == "toggle_sleep":
        msg, sleeping = actions.toggle_sleep(name)
        return {"msg": msg, "sleeping": sleeping}
    if action == "toggle_single_live":
        return {"msg": actions.toggle_single_live(name)}
    if action == "remove":
        return {"msg": actions.remove_channel(name)}
    if action == "set_quality":
        msg, restarted = actions.change_quality(name, quality)
        return {"msg": msg, "restarted": restarted}
    if action == "set_rerun_policy":
        return {"msg": actions.set_rerun_policy(name, data.get("policy") or None)}
    if action == "rerun_override":
        return {"msg": actions.rerun_override(name, data.get("verdict") or None)}
    raise ActionError("Azione non valida.")


@app.route("/api/room", methods=["POST"])
def api_room_action():
    data = request.get_json(silent=True) or {}
    platform = norm_platform(data.get("platform"))
    name = clean_name(platform, data.get("channel") or "")
    try:
        result = _dispatch(data.get("action", ""), name, platform,
                           data.get("quality", STREAM_QUALITY), data)
    except ActionError as exc:
        return jsonify({"ok": False, "msg": exc.msg}), exc.status
    result.setdefault("ok", True)
    return jsonify(result)


@app.route("/api/delete_recording", methods=["POST"])
def api_delete_recording():
    data = request.get_json(silent=True) or {}
    names = data.get("filenames") or [data.get("filename", "")]
    errors = []
    for name in names:
        ok, msg = storage.delete_recording(str(name))
        if not ok:
            errors.append(f"{name}: {msg}")
    if errors:
        return jsonify({"ok": False, "msg": "; ".join(errors)})
    return jsonify({"ok": True})


@app.route("/health")
def health():
    thread = state.monitor.thread
    alive = bool(thread and thread.is_alive())
    hb = time.time() - state.monitor.heartbeat if state.monitor.heartbeat else None
    monitor_state = "stopped" if not alive else ("degraded" if hb is not None and hb > 45 else "ok")
    return jsonify({
        "status": "ok" if alive else "degraded", "monitor_alive": alive, "monitor_state": monitor_state,
        "monitor_inflight": len(state.monitor.inflight),
    }), 200 if alive else 503


# ── Avvio ──

def _shutdown() -> None:
    for rec in list(state.recorders.values()):
        if rec.is_recording:
            rec.stop()
    preview.stop_all()
    monitor.shutdown()


def boot() -> None:
    state.init()
    with state.lock:
        for ch in state.channels:
            state.recorders[ch["name"]] = Recorder(ch["name"], ch.get("quality", STREAM_QUALITY),
                                                   ch.get("platform", DEFAULT_PLATFORM))
    monitor.start()
    status.start()
    atexit.register(_shutdown)
    logger.info("Twitch Recorder pronto — %d canali, dati in %s", len(state.channels), os.path.dirname(os.path.abspath(CHANNELS_FILE)))


boot()
