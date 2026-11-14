# SPDX-License-Identifier: GPL-2.0-only
"""Live-Ansicht im Browser: der Logger bleibt einziger Besitzer des PPK2.

feed() bekommt jeden dekodierten Block (µA, 100 kSps) und verdichtet ihn auf Min, Max und
Mittel je BUCKET Samples (100 = 1 ms). Die letzten HISTORY_S Sekunden liegen in einem
Ringpuffer. Ein kleiner HTTP-Server (nur Standardbibliothek) liefert die Seite
live_view.html und unter /stream die Daten als Server-Sent Events, ca. 10 Nachrichten pro
Sekunde je Browser.

feed() darf die Messung nie aufhalten: es rechnet nur ein paar numpy-Operationen und hält
die Sperre so kurz wie möglich. Langsame Browser bekommen ältere Daten nicht nachgeliefert,
sondern springen auf den aktuellen Stand (Lücke statt Rückstau).

Nur fürs Heimnetz gedacht: keine Anmeldung, kein TLS.
"""
import csv
import json
import os
import select
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "live_view.html")
PAGES = {"/": PAGE, "/index.html": PAGE, "/auswertung": os.path.join(HERE, "auswertung.html")}


def _nice():
    try:
        os.nice(10)          # Auswertung nachrangig, der Logger hat Vorrang
    except OSError:
        pass


class Analyser:
    """Startet analyse.py --json als eigenen Prozess (eigener Interpreter, kein GIL-Konflikt
    mit dem Lese-Thread), ein Lauf gleichzeitig, Ergebnis 60 s im Cache."""

    ALLOWED = ("hours", "von", "aktiv", "redraw-uah", "mah", "nutzbar", "tz")

    def __init__(self, data_dir, ttl=60.0, timeout=300.0):
        self.data_dir = data_dir
        self.ttl = ttl
        self.timeout = timeout
        self._lock = threading.Lock()
        self._cache = {}

    def run(self, params):
        args = []
        for k in self.ALLOWED:
            v = params.get(k)
            if v in (None, ""):
                continue
            if k == "von":
                v = str(v)[:19]
                if not all(c in "0123456789-:T " for c in v):
                    return {"error": "ungültiges Datum"}
            else:
                try:
                    v = repr(float(v))
                except ValueError:
                    return {"error": f"ungültiger Wert für {k}"}
            args += ["--" + k, v]
        key = tuple(args)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
            try:
                p = subprocess.run([sys.executable, os.path.join(HERE, "analyse.py"),
                                    "--dir", self.data_dir, "--json"] + args,
                                   capture_output=True, text=True, timeout=self.timeout,
                                   preexec_fn=_nice if os.name == "posix" else None)
                res = json.loads(p.stdout) if p.stdout.strip() else {
                    "error": (p.stderr.strip().splitlines() or ["analyse.py ohne Ausgabe"])[-1]}
            except subprocess.TimeoutExpired:
                res = {"error": "Auswertung dauert zu lange"}
            except (OSError, ValueError) as e:
                res = {"error": repr(e)}
            if "error" not in res:
                self._cache = {key: (time.time(), res)}
            return res


class WakeWorker:
    """wake_view.py --serve als dauerhafter Nebenprozess: lädt .npz-Dateien, verdichtet sie
    und hält die letzten im Speicher. Bei Absturz wird er neu gestartet."""

    def __init__(self, data_dir, timeout=60.0):
        self.data_dir = data_dir
        self.timeout = timeout
        self._p = None
        self._lock = threading.Lock()

    def _start(self):
        self._p = subprocess.Popen([sys.executable, os.path.join(HERE, "wake_view.py"),
                                    "--serve", "--dir", self.data_dir],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                   preexec_fn=_nice if os.name == "posix" else None)

    def request(self, q):
        with self._lock:
            for attempt in (0, 1):
                if self._p is None or self._p.poll() is not None:
                    self._start()
                try:
                    self._p.stdin.write(json.dumps(q) + "\n")
                    self._p.stdin.flush()
                    r, _, _ = select.select([self._p.stdout], [], [], self.timeout)
                    if not r:
                        raise TimeoutError
                    line = self._p.stdout.readline()
                    if not line:
                        raise BrokenPipeError
                    return json.loads(line)
                except (OSError, ValueError, TimeoutError):
                    self.close()
            return {"error": "Wachphasen-Prozess antwortet nicht"}

    def close(self):
        if self._p is not None:
            try:
                self._p.kill()
                self._p.wait(2)
            except Exception:
                pass
            self._p = None


def _fmt(a):
    return "[" + ",".join(f"{v:.5g}" for v in a.tolist()) + "]"


class LiveFeed:
    def __init__(self, bucket=100, history_s=120.0, rate=100_000):
        self.bucket = int(bucket)
        self.rate = int(rate)
        self.bucket_s = self.bucket / self.rate
        self.size = int(round(history_s / self.bucket_s))
        self._min = np.zeros(self.size, dtype=np.float32)
        self._max = np.zeros(self.size, dtype=np.float32)
        self._avg = np.zeros(self.size, dtype=np.float32)
        self.seq = 0          # Anzahl je geschriebener Buckets
        self.t_last = 0.0     # Wanduhrzeit des letzten Buckets
        self._rem = np.empty(0)
        self.status = {}      # Kennzahlen des Loggers (einmal pro Intervall)
        self.status_ver = 0
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

    def feed(self, samples, now):
        """Block anhängen. now = Wanduhrzeit des letzten Samples im Block."""
        x = np.concatenate((self._rem, samples)) if len(self._rem) else np.asarray(samples)
        k = len(x) // self.bucket
        self._rem = x[k * self.bucket:].copy()
        if k == 0:
            return
        b = x[: k * self.bucket].reshape(k, self.bucket)
        mn, mx, av = b.min(axis=1), b.max(axis=1), b.mean(axis=1)
        if k > self.size:
            mn, mx, av = mn[-self.size:], mx[-self.size:], av[-self.size:]
            skipped, k = k - self.size, self.size
        else:
            skipped = 0
        with self._cond:
            start = (self.seq + skipped) % self.size
            idx = (start + np.arange(k)) % self.size
            self._min[idx] = mn
            self._max[idx] = mx
            self._avg[idx] = av
            self.seq += skipped + k
            # Rest im Puffer liegt zeitlich nach dem letzten Bucket
            self.t_last = now - len(self._rem) / self.rate
            self._cond.notify_all()

    def set_status(self, d):
        """Kennzahlen des Loggers (Ruhestrom, Laufzeit, ...) für die Seite ablegen."""
        with self._cond:
            self.status = dict(d)
            self.status_ver += 1
            self._cond.notify_all()

    def since(self, cursor, max_n=None):
        """Buckets ab cursor (Sequenznummer). Gibt (start, ende, t_ende, min, max, mittel)."""
        with self._lock:
            end = self.seq
            start = max(cursor, end - self.size, 0)
            if max_n is not None:
                start = max(start, end - max_n)
            n = end - start
            idx = (start + np.arange(n)) % self.size
            return (start, end, self.t_last, self._min[idx].copy(), self._max[idx].copy(),
                    self._avg[idx].copy())

    def wait(self, cursor, timeout, status_ver=None):
        with self._cond:
            if self.seq <= cursor and (status_ver is None or status_ver == self.status_ver):
                self._cond.wait(timeout)


def read_wakes(path, n=50):
    """Letzte n Zeilen aus wake_index.csv als Liste von dicts (neueste zuerst)."""
    try:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    out = []
    for r in rows[-n:][::-1]:
        d = {}
        for k, v in r.items():
            try:
                d[k] = float(v) if k not in ("start_utc", "trigger_utc", "file", "status") else v
            except (TypeError, ValueError):
                d[k] = v
        out.append(d)
    return out


class LiveServer:
    def __init__(self, feed, port, host="0.0.0.0", interval=0.1, max_clients=4, info=None,
                 wake_index=None, data_dir=None):
        self.feed = feed
        self.data_dir = data_dir
        if wake_index is None and data_dir:
            wake_index = os.path.join(data_dir, "wake", "wake_index.csv")
        self.wake_index = wake_index
        self.analyser = Analyser(data_dir) if data_dir else None
        self.wake_worker = WakeWorker(data_dir) if data_dir else None
        self.interval = interval
        self.max_clients = max_clients
        self.info = info or {}
        self.clients = 0
        self._clients_lock = threading.Lock()
        self._stop = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                url = urlparse(self.path)
                if url.path in PAGES:
                    server._page(self, PAGES[url.path])
                elif url.path == "/api/analyse":
                    if not server.analyser:
                        server._json(self, {"error": "kein Datenordner"})
                        return
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    server._json(self, server.analyser.run(q))
                elif url.path == "/api/wake":
                    if not server.wake_worker:
                        server._json(self, {"error": "kein Datenordner"})
                        return
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    req = {"file": q.get("file", "")}
                    for k in ("a", "b", "n"):
                        if q.get(k, "").lstrip("-").isdigit():
                            req[k] = int(q[k])
                    if q.get("meta"):
                        req["meta"] = 1
                    server._json(self, server.wake_worker.request(req))
                elif url.path == "/stream":
                    q = parse_qs(url.query)
                    try:
                        back = float(q.get("s", ["10"])[0])
                    except ValueError:
                        back = 10.0
                    self.close_connection = True   # Stream belegt die Verbindung ganz
                    server._stream(self, back)
                elif url.path == "/wakes":
                    q = parse_qs(url.query)
                    try:
                        n = max(1, min(500, int(q.get("n", ["50"])[0])))
                    except ValueError:
                        n = 50
                    rows = read_wakes(server.wake_index, n) if server.wake_index else []
                    server._json(self, {"wakes": rows, "available": bool(server.wake_index)})
                elif url.path == "/info":
                    body = json.dumps(dict(server.info, bucket_s=feed.bucket_s,
                                           history_s=feed.size * feed.bucket_s,
                                           clients=server.clients)).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

        class Server(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                # Browser schließt Tab/Verbindung: kein Fehler, nicht ins Log
                import sys
                if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
                    super().handle_error(request, client_address)

        self.httpd = Server((host, port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="live-http",
                                        daemon=True)
        self._thread.start()

    def _json(self, h, obj):
        body = json.dumps(obj).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Cache-Control", "no-cache")
        h.end_headers()
        h.wfile.write(body)

    def _page(self, h, path=PAGE):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            h.send_error(500, os.path.basename(path) + " fehlt")
            return
        h.send_response(200)
        h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Cache-Control", "no-cache")
        h.end_headers()
        h.wfile.write(body)

    def _stream(self, h, back_s):
        with self._clients_lock:
            if self.clients >= self.max_clients:
                h.send_error(503, "zu viele Verbindungen")
                return
            self.clients += 1
        try:
            h.send_response(200)
            h.send_header("Content-Type", "text/event-stream")
            h.send_header("Cache-Control", "no-cache")
            h.send_header("Connection", "keep-alive")
            h.end_headers()
            f = self.feed
            back_n = int(max(0.0, back_s) / f.bucket_s)
            cursor = max(f.seq - back_n, 0)
            # Pro Nachricht höchstens 2 s, damit ein Rückstand nicht in einem Stück kommt
            chunk = int(2.0 / f.bucket_s)
            sent_status = -1
            while not self._stop.is_set():
                if f.status_ver != sent_status:
                    with f._lock:
                        st, sent_status = dict(f.status), f.status_ver
                    if st:
                        h.wfile.write(b"event: status\ndata: " + json.dumps(st).encode() + b"\n\n")
                        h.wfile.flush()
                start, end, t_end, mn, mx, av = f.since(cursor, max_n=None)
                if end > start:
                    if end - start > chunk:          # Rückstand: in Stücken senden
                        end2 = start + chunk
                        mn, mx, av = mn[:chunk], mx[:chunk], av[:chunk]
                        t_end = t_end - (end - end2) * f.bucket_s
                        end = end2
                    msg = ('{"start":%d,"end":%d,"t":%.4f,"dt":%g,"min":%s,"max":%s,"avg":%s}'
                           % (start, end, t_end, f.bucket_s, _fmt(mn), _fmt(mx), _fmt(av)))
                    h.wfile.write(b"data: " + msg.encode() + b"\n\n")
                    h.wfile.flush()
                    cursor = end
                    if f.seq - cursor > chunk:
                        continue
                else:
                    # Lebenszeichen, damit tote Verbindungen auffallen
                    h.wfile.write(b": ping\n\n")
                    h.wfile.flush()
                    f.wait(cursor, self.interval * 5, sent_status)
                time.sleep(self.interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self._clients_lock:
                self.clients -= 1

    def close(self):
        self._stop.set()
        if self.wake_worker:
            self.wake_worker.close()
        self.httpd.shutdown()
        self.httpd.server_close()


def _demo_history(d, hours=48.0):
    """Vorgeschichte als CSV wie vom Logger: Ruhestrom ~150 µA, 5-22 Uhr stündlich eine
    Wachphase (28 s), einmal eine günstige (8 s). Endet jetzt."""
    from datetime import datetime, timedelta, timezone
    rng = np.random.default_rng(3)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    t0 = now - timedelta(hours=hours)
    n = int(hours * 3600)
    ts = [t0 + timedelta(seconds=k) for k in range(n)]
    m = 150 + 8 * rng.standard_normal(n)
    for k, t in enumerate(ts):
        lh = (t.hour + 1) % 24
        if 5 <= lh <= 22 and t.minute == 0 and t.second < 28:
            m[k] = 30000 if t.second >= 6 else 60000
        elif lh == 9 and t.minute == 30 and t.second < 8:
            m[k] = 12000
    q, files = 0.0, {}
    for t, v in zip(ts, m):
        q += v / 3600
        name = os.path.join(d, f"ppk2_{t:%Y-%m-%d}.csv")
        if name not in files:
            f = open(name, "w", newline="")
            w = csv.writer(f)
            w.writerow(["timestamp_utc", "mean_uA", "min_uA", "max_uA", "samples",
                        "charge_uAh_total", "median_uA"])
            files[name] = (f, w)
        files[name][1].writerow([t.isoformat(timespec="seconds"), round(v, 3), 1.0,
                                 round(v * 3, 1), 100000, round(q, 4), round(v, 3)])
    for f, _ in files.values():
        f.close()


def _demo(port, data_dir=None):
    """Simulierter Prüfling ohne PPK2: gepulster Ruhestrom, alle 20 s eine Wachphase.
    Ohne data_dir: Vorgeschichte (48 h CSV) wird erzeugt, Wachphasen mit WakeRecorder
    aufgenommen. Mit data_dir: vorhandene Daten (z. B. vom Pi kopiert) nur anzeigen."""
    import tempfile
    from datetime import datetime, timezone
    from wake_capture import WakeRecorder
    rng = np.random.default_rng(0)
    feed = LiveFeed()
    own = data_dir is None
    if own:
        data_dir = tempfile.mkdtemp(prefix="ppk2demo")
        print("Erzeuge 48 h Vorgeschichte ...", flush=True)
        _demo_history(data_dir)
    rec = WakeRecorder(data_dir, pre_s=1, post_s=1, min_free_mb=0, log=lambda s: None) if own else None
    LiveServer(feed, port, host="127.0.0.1", info={"mode": "demo"}, data_dir=data_dir)
    print(f"Demo: http://127.0.0.1:{port}/  Auswertung: http://127.0.0.1:{port}/auswertung"
          f"  (Daten in {data_dir}, Strg+C beendet)", flush=True)
    rate, n, t0 = feed.rate, 0, time.time()
    charge, last_stat, buf = 0.0, t0, []
    csv_f = None
    while True:
        time.sleep(0.1)
        now = time.time()
        due = int((now - t0) * rate)
        k = due - n
        t = (n + np.arange(k)) / rate
        x = 150.0 * (1 + 0.05 * rng.standard_normal(k))
        x[(t % 0.7) < 0.001] = 30_000.0                     # Pulse des Wandlers
        ph = t % 20.0
        m1, m2 = (ph > 5) & (ph < 10), (ph >= 10) & (ph < 25)
        x[m1] = np.abs(60_000 * (1 + 0.3 * rng.standard_normal(int(m1.sum()))))
        x[m2] = 31_000 * (1 + 0.1 * rng.standard_normal(int(m2.sum())))
        n = due
        feed.feed(x, now)
        if rec:
            rec.feed(x, now)
        buf.append(x)
        charge += float(x.sum()) / rate / 3600.0
        if now - last_stat >= 1.0:
            a = np.concatenate(buf)
            buf = []
            if own:
                day = datetime.now(timezone.utc)
                path = os.path.join(data_dir, f"ppk2_{day:%Y-%m-%d}.csv")
                new = not os.path.exists(path)
                with open(path, "a", newline="") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["timestamp_utc", "mean_uA", "min_uA", "max_uA", "samples",
                                    "charge_uAh_total", "median_uA"])
                    w.writerow([day.isoformat(timespec="seconds"), round(float(a.mean()), 3),
                                round(float(a.min()), 3), round(float(a.max()), 3), len(a),
                                round(charge, 4), round(float(np.median(a)), 3)])
            run = now - t0
            avg = charge * 3600 / max(run, 1)
            st = {"charge_uAh": round(charge, 3), "runtime_h": run / 3600, "avg_total_uA": avg,
                  "quiet_avg_uA": 150.0 * 1.06, "est_days": 3000 * 1000 * 0.8 / avg / 24,
                  "samples": len(a), "interval_s": round(now - last_stat, 3),
                  "quiet_window_s": 300, "battery_mAh": 3000, "battery_usable": 0.8,
                  "wake_capture": bool(rec), "wake_trigger_uA": 5000, "wake_trigger_ms": 100}
            if rec:
                st["wake_count"] = rec.count
                st["wake_rec"] = rec._state == "rec"
                if st["wake_rec"]:
                    st["wake_since"] = rec._t_trigger
                if rec.last:
                    st["wake_last_uAh"] = rec.last["active_uAh"]
                    st["wake_last_s"] = rec.last["active_s"]
            feed.set_status(st)
            last_stat = now


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Live-Ansicht, ohne PPK2 nur als Demo")
    ap.add_argument("--demo", action="store_true", help="simulierte Daten ausliefern")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--dir", help="vorhandenen Datenordner anzeigen (z. B. vom Pi kopiert)")
    a = ap.parse_args()
    if not a.demo:
        ap.error("Ohne --demo läuft die Live-Ansicht nur im Logger (LIVE_PORT setzen)")
    _demo(a.port, a.dir)
