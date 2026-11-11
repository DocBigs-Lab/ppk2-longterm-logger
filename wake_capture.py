# SPDX-License-Identifier: GPL-2.0-only
"""Automatische Aufnahme von Wachphasen in voller Auflösung (100 kSps).

Der Logger reicht jeden dekodierten Block an WakeRecorder.feed() weiter. Der Recorder hält
die letzten Sekunden in einem Ringpuffer (Vorlauf). Steigt der gleitend gemittelte Strom
(Standard 100 ms) über die Schwelle, beginnt die Aufnahme. Das lange Fenster ist nötig, weil
der Ruhestrom vieler Schaltungen aus kurzen Pulsen besteht (Wandler im Pulsbetrieb): im
1-ms-Mittel bis ca. 30 mA, im 100-ms-Mittel unter 1 mA (gemessen am ESP32-C6-Display). Sie endet, wenn der Strom eine Nachlaufzeit lang
unter der Schwelle bleibt, spätestens nach der Höchstdauer. Gespeichert wird in einem
eigenen Thread als komprimierte .npz-Datei (float32, µA), dazu eine Zeile in wake_index.csv.

Zeitbasis: Am PPK2 kommen in der Praxis weniger als die nominalen 100 kSps an (gemessen ca.
88 kSps, Ursache offen). Der Recorder misst deshalb die effektive Rate (Samples pro
Wanduhr-Sekunde seit Start) und rechnet Dauer und Ladung damit. Vorlauf, Nachlauf,
Höchstdauer und Trigger-Fenster sind in Samples (nominal) bemessen und dadurch in Echtzeit
etwas länger.

Der Recorder darf die Messung nie aufhalten: Ist der Schreib-Thread noch belegt oder der
Speicher knapp, wird die Wachphase verworfen und nur im Index vermerkt.
"""
import csv
import json
import os
import queue
import shutil
import threading
from datetime import datetime, timezone

import numpy as np

FORMAT_VERSION = 2   # 2: rate_eff, Zeiten und Ladung mit effektiver Rate
INDEX_NAME = "wake_index.csv"
INDEX_HEADER = ["start_utc", "trigger_utc", "duration_s", "active_s", "pre_s",
                "charge_uAh", "active_uAh", "mean_uA", "peak_uA", "peak_1ms_uA",
                "samples", "truncated", "file", "status"]


def _utc(ts, timespec="milliseconds"):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec=timespec)


def _quiet_end(hot_idx, last_hot, n_seg, post_n):
    """Sucht das Ende einer Aktivität innerhalb eines Blocks.

    hot_idx:  Indizes (relativ zum Block) der Samples über der Schwelle, aufsteigend
    last_hot: letztes Sample über der Schwelle vor diesem Block (relativ, also negativ)
    Rückgabe: (Ende relativ zum Block oder None, letztes heißes Sample relativ)
    Das Ende liegt post_n Samples nach dem letzten heißen Sample, wenn dazwischen
    keines über der Schwelle liegt.
    """
    arr = np.concatenate(([last_hot], hot_idx)) if len(hot_idx) else np.array([last_hot])
    gaps = np.flatnonzero(np.diff(arr) > post_n)
    if len(gaps):
        h = int(arr[gaps[0]])
        return h + 1 + post_n, h
    h = int(arr[-1])
    if n_seg - 1 - h >= post_n:
        return h + 1 + post_n, h
    return None, h


class WakeRecorder:
    def __init__(self, out_dir, trigger_ua=5000.0, trigger_ms=100.0, pre_s=2.0, post_s=2.0,
                 max_s=120.0, max_mb=2000.0, min_free_mb=500.0, rate=100_000, meta=None,
                 log=None):
        self.dir = os.path.join(out_dir, "wake")
        self.trigger_ua = float(trigger_ua)
        self.rate = int(rate)
        self.k = max(1, int(round(trigger_ms * rate / 1000.0)))   # Fenster des Mittels
        self.pre_n = max(0, int(round(pre_s * rate)))
        self.post_n = max(1, int(round(post_s * rate)))
        self.max_n = max(1, int(round(max_s * rate)))
        self.max_bytes = max_mb * 1e6
        self.min_free = min_free_mb * 1e6
        self.meta = dict(meta or {})
        self.meta.update(trigger_uA=self.trigger_ua, trigger_ms=trigger_ms, pre_s=pre_s,
                         post_s=post_s, max_s=max_s)
        self.log = log or (lambda s: print(s, flush=True))
        os.makedirs(self.dir, exist_ok=True)

        self._ring = np.zeros(self.pre_n, np.float32)
        self._ring_pos = 0
        self._ring_fill = 0
        self._tail = np.zeros(0)          # letzte k-1 Samples für das gleitende Mittel
        self._state = "idle"              # idle | rec | blocked
        self._parts = []
        self._rec_n = 0                   # Samples in der laufenden Aufnahme
        self._pre = 0                     # davon Vorlauf
        self._last_hot = 0                # Index (Aufnahme) des letzten Samples über Schwelle
        self._cold = 0                    # blocked: Samples unter Schwelle am Stück
        self._t_trigger = 0.0
        self._fed = 0                     # Samples seit Start (für Ratenschätzung)
        self._first = None                # (Wanduhr, _fed) nach dem ersten Block
        self._now = 0.0                   # Wanduhr des letzten Blocks
        self._trig_mark = (0.0, 0)        # (Wanduhr, _fed) am Trigger-Block

        self.count = 0                    # abgeschlossene Wachphasen
        self.saved = 0
        self.dropped = 0
        self.last = None                  # Zusammenfassung der letzten Wachphase

        self._index_lock = threading.Lock()
        self._q = queue.Queue(maxsize=1)
        self._thread = threading.Thread(target=self._writer, name="wake-writer", daemon=True)
        self._thread.start()

    # ---------------------------------------------------------------- Ringpuffer
    def _ring_push(self, x):
        n = self.pre_n
        if n == 0 or len(x) == 0:
            return
        if len(x) >= n:
            self._ring[:] = x[-n:]
            self._ring_pos, self._ring_fill = 0, n
            return
        end = self._ring_pos + len(x)
        if end <= n:
            self._ring[self._ring_pos:end] = x
        else:
            first = n - self._ring_pos
            self._ring[self._ring_pos:] = x[:first]
            self._ring[:end - n] = x[first:]
        self._ring_pos = end % n
        self._ring_fill = min(n, self._ring_fill + len(x))

    def _ring_take(self):
        if self._ring_fill < self.pre_n:
            out = self._ring[:self._ring_fill].copy()
        else:
            out = np.concatenate((self._ring[self._ring_pos:], self._ring[:self._ring_pos]))
        self._ring_pos = self._ring_fill = 0
        return out

    # ---------------------------------------------------------------- Eingang
    def _hot(self, x):
        """Gleitendes Mittel über k Samples (blockübergreifend) > Schwelle."""
        ext = np.concatenate((self._tail, x.astype(np.float64)))
        cs = np.concatenate(([0.0], np.cumsum(ext)))
        idx = np.arange(len(self._tail), len(ext))
        start = np.maximum(0, idx - self.k + 1)
        cnt = idx + 1 - start
        avg = (cs[idx + 1] - cs[start]) / cnt
        self._tail = ext[-(self.k - 1):] if self.k > 1 else ext[:0]
        # Erst auslösen, wenn das Fenster voll ist. Sonst mittelt der Anfang nach dem Start
        # über wenige Samples, und ein einzelner Puls (z. B. 1 ms Wandler) löst aus.
        return (avg > self.trigger_ua) & (cnt >= self.k)

    def feed(self, samples, now):
        """Einen dekodierten Block (µA) verarbeiten. now = Wanduhrzeit des letzten Samples."""
        x = np.asarray(samples, dtype=np.float32)
        L = len(x)
        if L == 0:
            return
        hot = self._hot(x)
        self._fed += L
        self._now = now
        if self._first is None:
            self._first = (now, self._fed)
        pos = 0
        while pos < L:
            seg, hseg = x[pos:], hot[pos:]
            if self._state == "idle":
                h = np.flatnonzero(hseg)
                if not len(h):
                    self._ring_push(seg)
                    break
                i = int(h[0])
                pre = np.concatenate((self._ring_take(), seg[:i]))
                pre = pre[len(pre) - self.pre_n:] if self.pre_n else pre[:0]
                self._parts = [pre]
                self._rec_n = self._pre = self._last_hot = len(pre)
                self._t_trigger = now - (L - 1 - (pos + i)) / self.rate_eff()
                self._trig_mark = (now, self._fed - L)
                self._state = "rec"
                pos += i
            elif self._state == "rec":
                base = self._rec_n
                end, lh = _quiet_end(np.flatnonzero(hseg), self._last_hot - base,
                                     len(seg), self.post_n)
                self._last_hot = base + lh
                truncated = False
                limit = self._pre + self.max_n - base   # so viele Samples passen noch
                if (end is None or end > limit) and limit <= len(seg):
                    end, truncated = max(0, limit), True
                    self._last_hot = min(self._last_hot, self._pre + self.max_n - 1)
                if end is None:
                    self._parts.append(seg)
                    self._rec_n += len(seg)
                    break
                self._parts.append(seg[:end])
                self._rec_n += end
                self._finish(truncated, now, aborted=False)
                pos += end
                self._state = "blocked" if truncated else "idle"
                self._cold = 0
            else:  # blocked: nach Abbruch erst wieder scharf, wenn es ruhig geworden ist
                end, lh = _quiet_end(np.flatnonzero(hseg), -1 - self._cold, len(seg),
                                     self.post_n)
                if end is None:
                    self._cold = len(seg) - 1 - lh
                    self._ring_push(seg)
                    break
                self._ring_push(seg[:end])
                pos += end
                self._state = "idle"

    def rate_eff(self):
        """Effektive Samplerate seit Start (Samples pro Wanduhr-Sekunde).
        Bis 10 s Messdauer gilt die nominale Rate."""
        if self._first is None:
            return float(self.rate)
        t0, f0 = self._first
        dt = self._now - t0
        if dt < 10.0:
            return float(self.rate)
        return (self._fed - f0) / dt

    # ---------------------------------------------------------------- Abschluss
    def _finish(self, truncated, now, aborted):
        a = np.concatenate(self._parts)
        self._parts = []
        r = self.rate_eff()
        start = self._t_trigger - self._pre / r
        act = a[self._pre:self._last_hot + 1]
        cs = np.concatenate(([0.0], np.cumsum(a, dtype=np.float64)))
        k = min(self.k, len(a))
        peak_1ms = float(((cs[k:] - cs[:-k]) / k).max()) if len(a) else 0.0
        t_mark, fed_mark = self._trig_mark
        dt_wall = now - t_mark
        rate_est = (self._fed - fed_mark) / dt_wall if dt_wall > 0.5 else None
        ev = {
            "start_utc": _utc(start),
            "trigger_utc": _utc(self._t_trigger),
            "duration_s": round(len(a) / r, 5),
            "active_s": round(len(act) / r, 5),
            "pre_s": round(self._pre / r, 5),
            "charge_uAh": round(float(cs[-1]) / r / 3600.0, 4),
            "active_uAh": round(float(act.sum(dtype=np.float64)) / r / 3600.0, 4),
            "mean_uA": round(float(cs[-1]) / max(len(a), 1), 3),
            "peak_uA": round(float(a.max()) if len(a) else 0.0, 1),
            "peak_1ms_uA": round(peak_1ms, 1),
            "samples": int(len(a)),
            "truncated": int(truncated),
        }
        meta = dict(self.meta, format=FORMAT_VERSION, rate=self.rate, rate_eff=round(r, 1),
                    start_epoch=start,
                    trigger_epoch=self._t_trigger, pre_samples=self._pre,
                    active_samples=int(len(act)), aborted=aborted,
                    rate_est=round(rate_est, 1) if rate_est else None, **ev)
        self.count += 1
        self.last = ev
        # Rest der Aufnahme in den Ringpuffer: Vorlauf für eine direkt folgende Wachphase
        self._ring_push(a[len(a) - self.pre_n:] if self.pre_n else a[:0])
        try:
            self._q.put_nowait((ev, meta, a))
        except queue.Full:
            self.dropped += 1
            self._index_row(ev, "", "verworfen: Schreiber belegt")
            self.log(f"Wachphase {ev['trigger_utc']} verworfen (Schreiber noch belegt)")

    def wait_idle(self):
        """Warten, bis der Schreib-Thread alles gespeichert hat (für Tests)."""
        self._q.join()

    def close(self, timeout=30.0):
        """Laufende Aufnahme abschließen, Schreib-Thread beenden."""
        if self._state == "rec" and self._rec_n > self._pre:
            self._finish(False, self._trig_mark[0], aborted=True)
        self._state = "idle"
        try:
            self._q.put(None, timeout=timeout)
        except queue.Full:
            pass
        self._thread.join(timeout)

    # ---------------------------------------------------------------- Schreiben
    def _writer(self):
        while True:
            item = self._q.get()
            if item is None:
                self._q.task_done()
                return
            ev, meta, a = item
            try:
                self._save(ev, meta, a)
            except Exception as e:   # nie den Thread sterben lassen
                self.dropped += 1
                self.log(f"Wachphase {ev['trigger_utc']} nicht gespeichert: {e!r}")
            finally:
                self._q.task_done()

    def _save(self, ev, meta, a):
        if shutil.disk_usage(self.dir).free < self.min_free + a.nbytes:
            self.dropped += 1
            self._index_row(ev, "", "verworfen: wenig Speicherplatz")
            self.log(f"Wachphase {ev['trigger_utc']} verworfen (wenig Speicherplatz)")
            return
        t = datetime.fromtimestamp(meta["start_epoch"], timezone.utc)
        day = os.path.join(self.dir, f"{t:%Y-%m-%d}")
        os.makedirs(day, exist_ok=True)
        name = f"wake_{t:%Y%m%dT%H%M%S}Z"
        path, i = os.path.join(day, name + ".npz"), 2
        while os.path.exists(path):
            path, i = os.path.join(day, f"{name}_{i}.npz"), i + 1
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            np.savez_compressed(fh, uA=a, meta=np.array(json.dumps(meta)))
        os.replace(tmp, path)
        rel = os.path.relpath(path, self.dir)
        self.saved += 1
        self._index_row(ev, rel, "ok")
        self.log(f"Wachphase gespeichert: {rel}, {ev['active_s']:.1f} s aktiv, "
                 f"{ev['active_uAh']:.1f} µAh, {os.path.getsize(path) / 1e6:.1f} MB")
        self._enforce_limit()

    def _index_row(self, ev, rel, status):
        path = os.path.join(self.dir, INDEX_NAME)
        with self._index_lock:
            if os.path.exists(path):
                with open(path, newline="") as old:
                    if old.readline().strip() != ",".join(INDEX_HEADER):
                        os.rename(path, path + f".alt-{datetime.now():%Y%m%d%H%M%S}")
            new = not os.path.exists(path)
            with open(path, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(INDEX_HEADER)
                w.writerow([ev[k] for k in INDEX_HEADER[:-2]] + [rel, status])

    def _enforce_limit(self):
        """Älteste Dateien löschen, bis der Ordner unter max_mb liegt (neueste bleibt)."""
        files = []
        for root, _, names in os.walk(self.dir):
            files += [os.path.join(root, n) for n in names if n.endswith(".npz")]
        files.sort(key=os.path.basename)
        sizes = [os.path.getsize(p) for p in files]
        total = sum(sizes)
        for p, s in zip(files[:-1], sizes[:-1]):
            if total <= self.max_bytes:
                break
            os.remove(p)
            total -= s
            self.log(f"Speichergrenze: {os.path.relpath(p, self.dir)} gelöscht")
            d = os.path.dirname(p)
            if d != self.dir and not os.listdir(d):
                os.rmdir(d)
