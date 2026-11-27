"""Buffer circolare del transport stream: un solo produttore, molti lettori indipendenti.

Due accorgimenti tengono il player stabile:

* **Punto di ingresso su keyframe.** Un lettore nuovo non riceve il buffer dall'inizio
  (che di solito comincia a metà di un GOP, senza PAT/PMT): parte dall'ultimo keyframe
  video, preceduto da PAT e PMT. Il browser può decodificare subito il primo fotogramma,
  invece di restare su «Caricamento…» con audio e video disallineati.
* **Generazioni.** Durante una sessione di REC FFmpeg può riavviarsi (riconnessione,
  flusso fermo, split): il buffer resta lo stesso, ma ogni avvio apre una nuova
  "generazione". I lettori della generazione precedente vengono chiusi subito (il player
  si ricollega in un secondo allo stesso buffer) invece di ricevere timestamp e
  contatori che ripartono da capo, che bloccano la decodifica.
"""

import threading
import time
from collections import deque

from .config import LIVE_STREAM_BUFFER_BYTES

TS = 188


def _scan_packets(data: bytes):
    """Restituisce (pat, pmt_pids, pmt_packets, keyframes) di un blocco allineato a 188 byte.

    keyframes: lista di (offset_pacchetto, offset_pat_precedente_nello_stesso_blocco | None).
    """
    pat_pkt = None
    pmt_pids: set[int] = set()
    pmt_pkts: dict[int, bytes] = {}
    keyframes: list[tuple[int, int | None]] = []
    last_pat = None
    for i in range(0, len(data) - TS + 1, TS):
        b1 = data[i + 1]
        pid = ((b1 & 0x1F) << 8) | data[i + 2]
        pusi = b1 & 0x40
        if not pusi:
            continue
        afc = (data[i + 3] >> 4) & 3
        if pid == 0:
            last_pat = i
            pat_pkt = data[i:i + TS]
            pmt_pids |= _pat_pmt_pids(pat_pkt)
            continue
        if afc == 2:          # solo adaptation field, nessun payload
            continue
        offset = i + 4
        rai = False
        if afc == 3:
            af_len = data[i + 4]
            rai = af_len > 0 and bool(data[i + 5] & 0x40)
            offset += 1 + af_len
        if offset + 4 > i + TS:
            continue
        if data[offset:offset + 3] == b"\x00\x00\x01":
            # Solo i PES video (stream_id 0xE0–0xEF): per l'audio ogni frame è "chiave".
            if rai and 0xE0 <= data[offset + 3] <= 0xEF:
                keyframes.append((i, last_pat))
        else:
            # Sezione PSI: la teniamo se è una PMT annunciata dalla PAT.
            pmt_pkts[pid] = data[i:i + TS]
    return pat_pkt, pmt_pids, pmt_pkts, keyframes


def _pat_pmt_pids(pkt: bytes) -> set[int]:
    try:
        afc = (pkt[3] >> 4) & 3
        pos = 4 + (1 + pkt[4] if afc == 3 else 0)
        pos += 1 + pkt[pos]                    # pointer_field
        if pkt[pos] != 0x00:                   # table_id PAT
            return set()
        section_len = ((pkt[pos + 1] & 0x0F) << 8) | pkt[pos + 2]
        end = min(len(pkt), pos + 3 + section_len - 4)   # senza CRC
        pids = set()
        for j in range(pos + 8, end - 3, 4):
            program = (pkt[j] << 8) | pkt[j + 1]
            if program:
                pids.add(((pkt[j + 2] & 0x1F) << 8) | pkt[j + 3])
        return pids
    except IndexError:
        return set()


class LiveStreamHub:
    TS_PACKET = TS

    def __init__(self, max_bytes: int = LIVE_STREAM_BUFFER_BYTES):
        self.max_bytes = max(512 * 1024, int(max_bytes))
        # I chunk pubblicati sono sempre multipli di 188 byte e iniziano con un pacchetto
        # MPEG-TS intero: un lettore che si collega a metà o salta in avanti non riceve
        # mai un pacchetto spezzato.
        self._pending = b""
        self._cond = threading.Condition()
        self._chunks: deque[tuple[int, bytes]] = deque()   # (generazione, dati)
        self._first_seq = 1          # numero di sequenza del primo chunk nel buffer
        self._next_seq = 1
        self._bytes = 0
        self._closed = False
        self.generation = 0
        self._gen_start_seq = 1
        self._gen_ended = False      # il processo della generazione corrente è terminato
        # Ultimi PAT/PMT visti e keyframe della generazione corrente: (seq, offset, prefisso).
        self._pat = b""
        self._pmt_pids: set[int] = set()
        self._pmt: dict[int, bytes] = {}
        self._keyframes: deque[tuple[int, int, bytes]] = deque(maxlen=64)
        self.started_at = time.time()
        self.last_data_at = 0.0

    @property
    def closed(self) -> bool:
        return self._closed

    def new_generation(self) -> int:
        """Un nuovo processo FFmpeg sta per scrivere nel buffer: chiude i lettori del precedente."""
        with self._cond:
            self.generation += 1
            self._gen_start_seq = self._next_seq
            self._gen_ended = False
            self._pending = b""
            self._pat, self._pmt_pids, self._pmt = b"", set(), {}
            self._keyframes.clear()
            self._cond.notify_all()
            return self.generation

    def end_generation(self) -> None:
        """Il processo FFmpeg è terminato: chi si collega ora aspetta il prossimo avvio
        invece di ricevere gli ultimi secondi ormai vecchi."""
        with self._cond:
            self._gen_ended = True
            self._pending = b""

    def publish(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._cond:
            if self._closed:
                return
            data = self._pending + bytes(chunk)
            if self._next_seq == self._gen_start_seq and data and data[0] != 0x47:
                start = data.find(b"\x47")
                data = data[start:] if start >= 0 else b""
            cut = len(data) - len(data) % TS
            chunk, self._pending = data[:cut], data[cut:]
            if not chunk:
                return
            seq = self._next_seq
            self._index(seq, chunk)
            self._chunks.append((self.generation, chunk))
            self._next_seq += 1
            self._bytes += len(chunk)
            while self._bytes > self.max_bytes and len(self._chunks) > 1:
                self._bytes -= len(self._chunks.popleft()[1])
                self._first_seq += 1
            while self._keyframes and self._keyframes[0][0] < self._first_seq:
                self._keyframes.popleft()
            self.last_data_at = time.time()
            self._cond.notify_all()

    def _index(self, seq: int, chunk: bytes) -> None:
        try:
            pat, pmt_pids, pmt_pkts, keyframes = _scan_packets(chunk)
        except IndexError:
            return
        # Il prefisso usa le tabelle note *prima* di questo chunk: se PAT e PMT sono
        # nello stesso chunk prima del keyframe si parte direttamente da lì.
        prefix = self._pat + b"".join(self._pmt.values()) if self._pat else b""
        for offset, pat_offset in keyframes:
            if pat_offset is not None and offset - pat_offset <= 4 * TS:
                self._keyframes.append((seq, pat_offset, b""))
            else:
                self._keyframes.append((seq, offset, prefix))
        if pat:
            self._pat = pat
            self._pmt_pids |= pmt_pids
        for pid, pkt in pmt_pkts.items():
            if pid in self._pmt_pids:
                self._pmt[pid] = pkt

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def tail_bytes(self, nbytes: int) -> bytes:
        """Ultimi `nbytes` della generazione corrente (per estrarre una miniatura)."""
        with self._cond:
            parts, total = [], 0
            for gen, chunk in reversed(self._chunks):
                if gen != self.generation:
                    break
                parts.append(chunk)
                total += len(chunk)
                if total >= nbytes:
                    break
        return b"".join(reversed(parts))

    def _join_point(self) -> tuple[int, int, bytes]:
        """(seq, offset, prefisso) da cui far partire un lettore nuovo. Chiamare con il lock."""
        if self._keyframes:
            seq, offset, prefix = self._keyframes[-1]
            if seq >= self._first_seq:
                return seq, offset, prefix
        # Nessun keyframe riconosciuto: inizio della generazione (FFmpeg comincia con PAT/PMT)
        # oppure, se è già uscito dal buffer, il chunk più vecchio rimasto.
        return max(self._gen_start_seq, self._first_seq), 0, b""

    def iter_chunks(self, from_start: bool = True, timeout: float = 15, keyframe: bool = False):
        """Invia i dati recenti e poi segue la diretta.

        keyframe=True  → parte dall'ultimo keyframe con PAT/PMT (consigliato per i player);
                         se la generazione cambia il generatore termina.
        from_start     → (senza keyframe) invia tutto il buffer disponibile.
        Un lettore lento che resta indietro oltre la capienza del buffer salta in avanti
        invece di bloccare il produttore. Durante l'inattività produce b"".
        """
        first_payload = b""
        offset = 0
        sent = False
        with self._cond:
            if keyframe:
                gen = self.generation
                if self._gen_ended:
                    # Tra un avvio di FFmpeg e il successivo: si aspetta la prossima generazione.
                    cursor = self._next_seq
                else:
                    cursor, offset, first_payload = self._join_point()
            else:
                gen = None
                cursor = self._first_seq if from_start else self._next_seq
        while True:
            with self._cond:
                while True:
                    if gen is not None and self.generation != gen:
                        if sent:
                            return
                        # Nessun dato ancora inviato: ci si aggancia direttamente al nuovo avvio.
                        gen = self.generation
                        cursor, offset, first_payload = self._join_point()
                    if cursor < self._first_seq:
                        cursor, offset = self._first_seq, 0
                    if cursor < self._next_seq:
                        chunk_gen, payload = self._chunks[cursor - self._first_seq]
                        cursor += 1
                        if gen is not None and chunk_gen != gen:
                            # Dato di una generazione precedente rimasto nel buffer: saltalo.
                            offset, first_payload = 0, b""
                            continue
                        if offset:
                            payload = payload[offset:]
                            offset = 0
                        if first_payload:
                            payload = first_payload + payload
                            first_payload = b""
                        break
                    if self._closed:
                        return
                    if not self._cond.wait(timeout=timeout):
                        payload = None
                        break
            if payload is None:
                yield b""   # segnale di inattività: il chiamante decide se chiudere
                continue
            sent = True
            yield payload
