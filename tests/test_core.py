import json
import time

import pytest

from twrec import rerun, state, status
from twrec.checks import CheckResult
from twrec.hub import LiveStreamHub
from twrec.quality import fallback_list, parse_opened_quality
from twrec.util import clean_name, normalize_title
from twrec.web import app


@pytest.fixture()
def client():
    return app.test_client()


def _api(client, **body):
    return client.post("/api/room", json=body).get_json()


# ── Qualità ──

def test_fallback_same_height_first_then_lower_then_higher():
    order = fallback_list("720p60").split(",")
    assert order[0] == "720p60"
    assert order.index("720p") < order.index("480p")
    assert order.index("480p") < order.index("1080p60")
    assert order[-1] == "best"


def test_fallback_without_fps_prefers_plain_variant():
    order = fallback_list("1080p").split(",")
    assert order[:2] == ["1080p", "1080p30"]
    assert order.index("1080p60") < order.index("720p")


def test_aliases_pass_through():
    assert fallback_list("best") == "best"
    assert fallback_list("audio_only") == "audio_only"


def test_parse_opened_quality():
    assert parse_opened_quality("[cli][info] Opening stream: 720p60 (hls-multivariant)") == "720p60"
    assert parse_opened_quality("[cli][info] Found matching plugin twitch") is None


# ── Utilità ──

def test_clean_name_from_url():
    assert clean_name("twitch", "https://www.twitch.tv/SomeOne?x=1") == "someone"
    assert clean_name("youtube", "https://youtube.com/@Canale/live") == "Canale"


def test_normalize_title_ignores_commands_and_dates():
    assert normalize_title("Live 12/10!! !discord !prime #ad") == normalize_title("live 13/10 !merch")


# ── Buffer condiviso ──

def _ts(n_packets: int, marker: int) -> bytes:
    return (b"\x47" + bytes([marker]) * 187) * n_packets


def test_hub_slow_reader_skips_ahead():
    hub = LiveStreamHub(max_bytes=512 * 1024)
    reader = hub.iter_chunks(from_start=True, timeout=0.05)
    for i in range(40):
        hub.publish(_ts(350, i))
    first = next(reader)
    assert first[1] >= 30   # i chunk più vecchi sono stati scartati, il lettore salta in avanti
    hub.close()


def test_hub_chunks_are_packet_aligned():
    hub = LiveStreamHub(max_bytes=1024 * 1024)
    stream = b"xx" + _ts(100, 1)               # spazzatura iniziale e letture non allineate
    for i in range(0, len(stream), 1000):
        hub.publish(stream[i:i + 1000])
    reader = hub.iter_chunks(from_start=True, timeout=0.05)
    hub.close()
    chunks = [c for c in reader if c]
    assert all(len(c) % 188 == 0 and c[0] == 0x47 for c in chunks)
    assert sum(map(len, chunks)) == 188 * 100


def test_hub_tail_bytes():
    hub = LiveStreamHub(max_bytes=1024 * 1024)
    for i in range(5):
        hub.publish(_ts(6, i))
    tail = hub.tail_bytes(1500)
    assert tail[1] in (3, 4) and len(tail) % 188 == 0


# ── Rerun ──

def _history(name, sessions):
    rerun._load()
    rerun._history[name] = {"sessions": sessions, "ignore_titles": []}


def test_tagged_rerun_is_confirmed():
    result = rerun.on_live("tagtest", {"title": "Rerun: ieri sera", "stream_id": "1"}, tagged=True)
    assert result["level"] == "confirmed"


def test_title_matching_old_live_is_suspect():
    now = time.time()
    _history("dup", [{"id": "old", "stream_id": "9", "started_at": now - 3 * 86400, "ended_at": now - 3 * 86400 + 7200,
                      "titles": ["Maratona Elden Ring tutti i boss senza morire"], "game": "Elden Ring",
                      "peak_viewers": 500, "override": None}])
    result = rerun.on_live("dup", {"title": "Maratona Elden Ring tutti i boss senza morire", "game": "Elden Ring",
                                   "stream_id": "10", "started_at": now}, tagged=False, now=now)
    assert result["score"] >= 55
    assert result["level"] in ("suspect", "probable")
    assert any("identico" in r["text"] for r in result["reasons"])


def test_recurring_title_is_not_counted():
    now = time.time()
    sessions = [{"id": str(i), "stream_id": str(i), "started_at": now - (i + 1) * 86400,
                 "ended_at": now - (i + 1) * 86400 + 3600, "titles": ["Giochiamo insieme con la community"],
                 "game": "", "peak_viewers": 100, "override": None} for i in range(6)]
    _history("fixed", sessions)
    result = rerun.on_live("fixed", {"title": "Giochiamo insieme con la community", "stream_id": "new",
                                     "started_at": now}, tagged=False, now=now)
    assert result["level"] == "none"


def test_restart_after_crash_is_not_a_rerun():
    now = time.time()
    _history("crash", [{"id": "a", "stream_id": "a", "started_at": now - 3600, "ended_at": now - 600,
                        "titles": ["Torneo serale di scacchi con gli iscritti"], "game": "", "peak_viewers": 50,
                        "override": None}])
    result = rerun.on_live("crash", {"title": "Torneo serale di scacchi con gli iscritti", "stream_id": "b",
                                     "started_at": now}, tagged=False, now=now)
    assert result["level"] == "none"


def test_keyword_and_override():
    result = rerun.on_live("kw", {"title": "REPLICA della live di ieri", "stream_id": "k1"}, tagged=False)
    assert result["level"] == "suspect"
    result = rerun.set_override("kw", "original")
    assert result["level"] == "none"
    assert normalize_title("REPLICA della live di ieri") in rerun._history["kw"]["ignore_titles"]


def test_decide_policies():
    confirmed = {"level": "confirmed"}
    probable = {"level": "probable"}
    suspect = {"level": "suspect"}
    assert rerun.decide({"rerun_policy": "skip"}, probable) == "skip"
    assert rerun.decide({"rerun_policy": "mark"}, probable) == "mark"
    assert rerun.decide({"rerun_policy": "record"}, suspect) == "record"
    assert rerun.decide({"rerun_policy": "mark"}, confirmed) == "mark"
    assert rerun.decide({"rerun_policy": "skip"}, suspect) == "mark"


# ── API e monitor ──

def test_add_pause_quality_remove(client):
    r = _api(client, action="add", channel="https://twitch.tv/OffTest", platform="twitch", quality="720p")
    assert r["ok"], r
    assert state.find_channel("offtest")["quality"] == "720p"
    assert _api(client, action="add", channel="offtest", platform="twitch")["ok"] is False
    assert _api(client, action="pause", channel="offtest")["ok"]
    assert state.find_channel("offtest")["is_recording"] is False
    r = _api(client, action="set_quality", channel="offtest", quality="1080p60")
    assert r["ok"] and r["restarted"] is False
    assert _api(client, action="set_quality", channel="offtest", quality="9000p")["ok"] is False
    assert _api(client, action="set_rerun_policy", channel="offtest", policy="skip")["ok"]
    assert state.find_channel("offtest")["rerun_policy"] == "skip"
    assert _api(client, action="remove", channel="offtest")["ok"]
    assert state.find_channel("offtest") is None


def test_bulk_and_sleep(client):
    for n in ("bulka", "bulkb"):
        _api(client, action="add", channel=n, platform="kick")
    r = _api(client, action="bulk", channels=["bulka", "bulkb", "missing"], op="sleep")
    assert r["done"] == 2 and len(r["failed"]) == 1
    assert state.find_channel("bulka")["sleeping"] is True
    r = _api(client, action="bulk", channels=["bulka", "bulkb"], op="remove")
    assert r["ok"] and state.find_channel("bulka") is None


def test_offline_needs_confirmations(client):
    _api(client, action="add", channel="flappy", platform="kick")
    with state.lock:
        state.find_channel("flappy")["sleeping"] = False
    from twrec.monitor import apply_check_result
    apply_check_result("flappy", CheckResult(True, True, "live", meta={"title": "Ciao"}))
    assert state.find_channel("flappy")["last_status"] == "online"
    apply_check_result("flappy", CheckResult(False, True, "offline"))
    assert state.find_channel("flappy")["last_check_result"] == "verifying_offline"
    apply_check_result("flappy", CheckResult(False, False, "error", "timeout"))
    assert state.online_status["flappy"] is True          # l'errore non cambia lo stato confermato
    for _ in range(3):
        apply_check_result("flappy", CheckResult(False, True, "offline"))
    assert state.find_channel("flappy")["last_status"] == "offline"
    _api(client, action="remove", channel="flappy")


def test_skip_policy_marks_rerun_status(client):
    _api(client, action="add", channel="skipme", platform="kick")
    _api(client, action="set_rerun_policy", channel="skipme", policy="skip")
    from twrec.monitor import apply_check_result
    apply_check_result("skipme", CheckResult(True, True, "live", meta={"title": "x"}, tagged_rerun=True))
    ch = state.find_channel("skipme")
    assert ch["last_status"] == "rerun" and not state.online_status["skipme"]
    _api(client, action="remove", channel="skipme")


def test_export_import_roundtrip(client):
    _api(client, action="add", channel="expone", platform="youtube", quality="480p")
    data = client.get("/api/export?format=json").get_data(as_text=True)
    _api(client, action="remove", channel="expone")
    r = client.post("/api/import", data=data, content_type="application/json").get_json()
    assert r["imported"] >= 1
    ch = state.find_channel("expone")
    assert ch["platform"] == "youtube" and ch["quality"] == "480p"
    csv_text = client.get("/api/export?format=csv").get_data(as_text=True)
    assert csv_text.splitlines()[0].startswith("name,platform,quality")
    _api(client, action="remove", channel="expone")


def test_status_and_delta(client):
    full = client.get("/api/status").get_json()
    assert {"channels", "recordings", "stats", "config", "disk"} <= set(full)
    q, initial = status.subscribe()
    try:
        assert initial.startswith("event: status")
        _api(client, action="add", channel="deltatest", platform="kick")
        status._broadcast_once()
        msg = q.get(timeout=2)
        payload = json.loads(msg.split("data: ", 1)[1])
        assert "deltatest" in [c["name"] for c in payload["channels_changed"]]
        status._broadcast_once()
        payload = json.loads(q.get(timeout=2).split("data: ", 1)[1])
        assert payload["channels_changed"] == []         # nessun cambiamento: nessun canale reinviato
    finally:
        status.unsubscribe(q)
        _api(client, action="remove", channel="deltatest")


def test_recording_paths_are_safe(client):
    assert client.get("/recordings/..%2Fchannels.json").status_code in (400, 404)
    r = client.post("/api/delete_recording", json={"filename": "../channels.json"}).get_json()
    assert r["ok"] is False


def test_index_and_health(client):
    page = client.get("/")
    assert page.status_code == 200 and b"app.js" in page.data
    assert client.get("/health").status_code == 200


def test_helix_batch_parsing(monkeypatch):
    from twrec import checks, twitch_api
    calls = []

    def fake_get(path, params):
        calls.append((path, params))
        return {"data": [{"user_login": "alpha", "title": "Rerun di ieri", "game_name": "Chess",
                          "viewer_count": 12, "started_at": "2026-11-07T18:00:00Z", "id": "s1",
                          "user_id": "u1", "tags": ["Italiano"], "type": "live"}]}

    monkeypatch.setattr(twitch_api, "_get", fake_get)
    names = ["alpha"] + [f"c{i}" for i in range(150)]
    out = checks.check_twitch_batch(names)
    assert len(calls) == 2                          # 151 canali -> 2 richieste
    assert out["alpha"].online and out["alpha"].tagged_rerun and out["alpha"].via == "helix"
    assert out["alpha"].meta["viewers"] == 12
    assert out["c5"].certain and not out["c5"].online


def test_low_disk_blocks_new_recordings(client, monkeypatch):
    from twrec import storage
    from twrec.monitor import apply_check_result
    _api(client, action="add", channel="diskfull", platform="kick")
    monkeypatch.setattr(storage, "has_free_space", lambda: False)
    apply_check_result("diskfull", CheckResult(True, True, "live", meta={"title": "x"}))
    assert not state.recorders["diskfull"].is_recording
    assert "Spazio libero insufficiente" in state.find_channel("diskfull")["last_error"]
    _api(client, action="remove", channel="diskfull")


def test_channel_log_rotation_and_disable(monkeypatch, tmp_path):
    from twrec import recorder
    monkeypatch.setattr(recorder, "RECORDINGS_DIR", str(tmp_path))
    monkeypatch.setattr(recorder, "RECORDING_LOG_MAX_MB", 0.001)       # ~1 KB
    (tmp_path / ".canale.log").write_text("x" * 5000)
    fh = recorder.open_channel_log("canale")
    fh.write("nuovo\n"); fh.close()
    assert (tmp_path / ".canale.log.1").stat().st_size == 5000
    assert (tmp_path / ".canale.log").read_text() == "nuovo\n"
    monkeypatch.setattr(recorder, "RECORDING_LOGS", False)
    assert recorder.open_channel_log("canale") is None


def test_recording_thumbs_stay_in_memory(tmp_path, monkeypatch):
    import os
    import shutil
    import subprocess
    from twrec import storage
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg non disponibile")
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    monkeypatch.setattr(storage, "RECORDINGS_DIR", str(rec_dir))
    video = rec_dir / "canale_2026-11-08_10-00-00.ts"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=10", "-t", "4", "-c:v", "libx264", "-g", "10", str(video)], check=True)
    first = storage.recording_thumb(video.name)
    assert first and first[:2] == b"\xff\xd8"                   # JPEG
    assert storage.recording_thumb(video.name) is first          # seconda volta dalla cache
    assert sorted(os.listdir(rec_dir)) == [video.name]           # nessun file accanto al video


def test_legacy_files_cleanup(tmp_path, monkeypatch):
    from twrec import storage
    rec_dir = tmp_path / "rec"
    (rec_dir / ".thumbs").mkdir(parents=True)
    (rec_dir / ".thumbs" / "x.ts.jpg").write_bytes(b"jpg")
    (rec_dir / ".recordings_meta.json").write_text('{"a.ts": {"title": "t"}}')
    (rec_dir / ".canale.log").write_text("log")
    (rec_dir / "a.ts").write_bytes(b"video")
    new_meta = tmp_path / "recordings_meta.json"
    monkeypatch.setattr(storage, "RECORDINGS_DIR", str(rec_dir))
    monkeypatch.setattr(storage, "LEGACY_THUMBS_DIR", str(rec_dir / ".thumbs"))
    monkeypatch.setattr(storage, "LEGACY_META_FILE", str(rec_dir / ".recordings_meta.json"))
    monkeypatch.setattr(storage, "RECORDINGS_META_FILE", str(new_meta))
    import twrec.config as cfg
    monkeypatch.setattr(cfg, "RECORDING_LOGS", False)
    storage.cleanup_legacy_files()
    assert [p.name for p in rec_dir.iterdir()] == ["a.ts"]
    assert "title" in new_meta.read_text()
