"""Ambiente isolato: dati in una cartella temporanea e uno Streamlink finto nel PATH."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = tempfile.mkdtemp(prefix="twrec-test-")
os.environ["CHANNELS_FILE"] = os.path.join(DATA, "channels.json")
os.environ["RECORDINGS_DIR"] = os.path.join(DATA, "recordings")
FAKE_BIN = os.path.join(ROOT, "tests", "fake_bin")
# Il permesso di esecuzione può andare perso (checkout su Windows, patch applicate a mano): senza,
# verrebbe usato lo Streamlink vero installato nel sistema e i test contatterebbero le piattaforme.
for _name in os.listdir(FAKE_BIN):
    _path = os.path.join(FAKE_BIN, _name)
    if os.path.isfile(_path):
        os.chmod(_path, os.stat(_path).st_mode | 0o755)
os.environ["PATH"] = FAKE_BIN + os.pathsep + os.environ.get("PATH", "")
os.environ.setdefault("APP_PASSWORD", "")
sys.path.insert(0, ROOT)
