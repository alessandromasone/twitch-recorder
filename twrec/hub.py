"""Buffer circolare in memoria: un solo produttore, molti lettori indipendenti."""

import threading
import time
from collections import deque

from .config import LIVE_STREAM_BUFFER_BYTES


class LiveStreamHub:
    def __init__(self, max_bytes: int = LIVE_STREAM_BUFFER_BYTES):
        self.max_bytes = max(512 * 1024, int(max_bytes))
        self._cond = threading.Condition()
        self._chunks: deque[bytes] = deque()
        self._first_seq = 1          # numero di sequenza del primo chunk nel buffer
        self._next_seq = 1
        self._bytes = 0
        self._closed = False
        self.started_at = time.time()
        self.last_data_at = 0.0

    @property
    def closed(self) -> bool:
        return self._closed

    def publish(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._cond:
            if self._closed:
                return
            self._chunks.append(bytes(chunk))
            self._next_seq += 1
            self._bytes += len(chunk)
            while self._bytes > self.max_bytes and len(self._chunks) > 1:
                self._bytes -= len(self._chunks.popleft())
                self._first_seq += 1
            self.last_data_at = time.time()
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def tail_bytes(self, nbytes: int) -> bytes:
        """Ultimi `nbytes` del buffer (per estrarre una miniatura)."""
        with self._cond:
            parts, total = [], 0
            for chunk in reversed(self._chunks):
                parts.append(chunk)
                total += len(chunk)
                if total >= nbytes:
                    break
        return b"".join(reversed(parts))

    def iter_chunks(self, from_start: bool = True, timeout: float = 15):
        """Invia il buffer recente (se richiesto) e poi segue i dati live.

        Un lettore lento che resta indietro oltre la capienza del buffer salta in avanti
        invece di bloccare il produttore.
        """
        with self._cond:
            cursor = self._first_seq if from_start else self._next_seq
        while True:
            with self._cond:
                while True:
                    if cursor < self._first_seq:
                        cursor = self._first_seq
                    if cursor < self._next_seq:
                        payload = self._chunks[cursor - self._first_seq]
                        cursor += 1
                        break
                    if self._closed:
                        return
                    if not self._cond.wait(timeout=timeout):
                        payload = None
                        break
            if payload is None:
                yield b""   # segnale di inattività: il chiamante decide se chiudere
                continue
            yield payload
