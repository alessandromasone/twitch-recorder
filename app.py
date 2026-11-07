"""Twitch Recorder — punto d'ingresso (gunicorn app:app oppure python app.py)."""

import signal
import sys

from twrec.config import PORT, logger
from twrec.web import app  # noqa: F401  (avvia monitor e broadcaster)


def _shutdown(sig, _frame):
    logger.info("Segnale %s — chiusura…", sig)
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
