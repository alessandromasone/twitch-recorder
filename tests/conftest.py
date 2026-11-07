"""Ambiente isolato: dati in una cartella temporanea e uno Streamlink finto nel PATH."""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = tempfile.mkdtemp(prefix="twrec-test-")
os.environ["CHANNELS_FILE"] = os.path.join(DATA, "channels.json")
os.environ["RECORDINGS_DIR"] = os.path.join(DATA, "recordings")
os.environ["PATH"] = os.path.join(ROOT, "tests", "fake_bin") + os.pathsep + os.environ.get("PATH", "")
os.environ.setdefault("APP_PASSWORD", "")
sys.path.insert(0, ROOT)
