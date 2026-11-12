"""Configurazione Gunicorn (letta in automatico dalla cartella di lavoro).

Allo stop del container (SIGTERM) le registrazioni vengono chiuse subito e in parallelo,
mentre Gunicorn attende le richieste aperte: così i file vengono finalizzati prima dello
stop forzato invece di restare da recuperare al riavvio.
"""

import signal


def post_worker_init(worker):
    previous = signal.getsignal(signal.SIGTERM)

    def on_term(signum, frame):
        try:
            from twrec.web import shutdown_in_background
            shutdown_in_background()
        except Exception:  # pragma: no cover - la chiusura di Gunicorn deve proseguire comunque
            pass
        if callable(previous):
            previous(signum, frame)

    signal.signal(signal.SIGTERM, on_term)
