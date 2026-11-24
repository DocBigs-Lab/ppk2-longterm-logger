# SPDX-License-Identifier: GPL-2.0-only
"""Live-Ansicht im Browser: der Logger bleibt einziger Besitzer des PPK2.

feed() bekommt jeden dekodierten Block (µA, 100 kSps) und verdichtet ihn auf Min, Max und
Mittel je BUCKET Samples (100 = 1 ms). Die letzten HISTORY_S Sekunden liegen in einem
Ringpuffer. Dazu eine grobe Stufe: je COARSE Buckets (100 = 100 ms) Min, Max, Mittel und
Uhrzeit des Endes, LONG_S Sekunden lang (Standard 24 h, ca. 17 MB), abrufbar binär unter
/verlauf. Ein kleiner HTTP-Server (nur Standardbibliothek) liefert die Seite
live_view.html und unter /stream die Daten als Server-Sent Events, ca. 10 Nachrichten pro
Sekunde je Browser.

feed() darf die Messung nie aufhalten: es rechnet nur ein paar numpy-Operationen und hält
die Sperre so kurz wie möglich. Langsame Browser bekommen ältere Daten nicht nachgeliefert,
sondern springen auf den aktuellen Stand (Lücke statt Rückstau).

Mit Projektverwaltung (projects.py) gibt es unter /projekte eine Seite zum Anlegen,
Wechseln und Ändern von Projekten (POST /api/projekte). Auswertung und Wachphasen nehmen
?projekt=<slug>, ohne den Parameter gilt das aktive Projekt.

Nur fürs Heimnetz gedacht: kein TLS. Änderungen lassen sich mit ADMIN_TOKEN schützen.
"""
import csv
import hmac
import json
import os
import select
import subprocess
import sys
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "live_view.html")
PAGES = {"/": PAGE, "/index.html": PAGE, "/auswertung": os.path.join(HERE, "auswertung.html"),
         "/projekte": os.path.join(HERE, "projekte.html")}
SUMMARY = "zusammenfassung.json"   # Kennzahlen eines archivierten Projekts (Cache)


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


def _csv_sig(d):
    """Kennung des CSV-Stands eines Ordners (Anzahl, Größe, letzte Änderung)."""
    n = size = mt = 0
    try:
        names = os.listdir(d)
    except OSError:
        return "0"
    for nm in names:
        if nm.startswith("ppk2_") and nm.endswith(".csv"):
            st = os.stat(os.path.join(d, nm))
            n, size, mt = n + 1, size + st.st_size, max(mt, int(st.st_mtime))
    return f"{n}-{size}-{mt}"


class Summaries:
    """Kennzahlen über die ganze Messdauer eines Projekts (analyse.py --json, im
    Hintergrund, eins nach dem anderen). Ergebnis als zusammenfassung.json im Projekt;
    neu berechnet, wenn sich die CSV-Dateien geändert haben."""

    KEYS = ("start", "end", "measured_h", "avg_uA", "per_day_mAh", "days", "wake_uAh",
            "total_uAh")

    def __init__(self):
        self._busy = set()
        self._lock = threading.Lock()
        self._q = []
        self._thread = None

    def get(self, d, settings):
        sig = _csv_sig(d)
        try:
            with open(os.path.join(d, SUMMARY), encoding="utf-8") as f:
                res = json.load(f)
            if res.get("sig") == sig:
                return res
        except (OSError, ValueError):
            pass
        if sig.startswith("0-"):
            return {"leer": True}
        with self._lock:
            if d not in self._busy:
                self._busy.add(d)
                self._q.append((d, dict(settings), sig))
                if not self._thread or not self._thread.is_alive():
                    self._thread = threading.Thread(target=self._work, name="summaries",
                                                    daemon=True)
                    self._thread.start()
        return {"berechne": True}

    def _work(self):
        while True:
            with self._lock:
                if not self._q:
                    return
                d, st, sig = self._q.pop(0)
            try:
                p = subprocess.run(
                    [sys.executable, os.path.join(HERE, "analyse.py"), "--dir", d, "--json",
                     "--hours", "1000000", "--aktiv", repr(st["quiet_max_uA"]),
                     "--mah", repr(st["battery_mAh"]), "--nutzbar", repr(st["battery_usable"]),
                     "--redraw-uah", repr(st.get("redraw_uAh", 120.0))],
                    capture_output=True, text=True, timeout=900,
                    preexec_fn=_nice if os.name == "posix" else None)
                r = json.loads(p.stdout) if p.stdout.strip() else {"error": "keine Ausgabe"}
                res = {"sig": sig}
                if "error" in r:
                    res["error"] = r["error"]
                else:
                    res.update({k: r.get(k) for k in self.KEYS})
                    res["quiet_uA"] = (r.get("quiet") or {}).get("mean_uA")
                    res["wakes"] = len(r.get("wakes") or [])
                tmp = os.path.join(d, SUMMARY + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(res, f)
                os.replace(tmp, os.path.join(d, SUMMARY))
            except Exception as e:      # nur Anzeige, nie den Server stören
                print(f"Zusammenfassung {d}: {e!r}", file=sys.stderr, flush=True)
            finally:
                with self._lock:
                    self._busy.discard(d)


def _fmt(a):
    return "[" + ",".join(f"{v:.5g}" for v in a.tolist()) + "]"


class LiveFeed:
    def __init__(self, bucket=100, history_s=120.0, rate=100_000, coarse=100, long_s=86400.0):
        self.bucket = int(bucket)
        self.rate = int(rate)
        self.bucket_s = self.bucket / self.rate
        self.size = int(round(history_s / self.bucket_s))
        self._min = np.zeros(self.size, dtype=np.float32)
        self._max = np.zeros(self.size, dtype=np.float32)
        self._avg = np.zeros(self.size, dtype=np.float32)
        # Grobe Stufe: Bucket j fasst die feinen Buckets j*coarse … (j+1)*coarse-1 zusammen
        self.coarse = int(coarse)
        self.coarse_s = self.bucket_s * self.coarse
        self.csize = max(2, int(round(long_s / self.coarse_s)))
        self._cmin = np.full(self.csize, np.nan, dtype=np.float32)
        self._cmax = np.full(self.csize, np.nan, dtype=np.float32)
        self._cavg = np.full(self.csize, np.nan, dtype=np.float32)
        self._ct = np.full(self.csize, np.nan)      # Wanduhrzeit am Ende des Buckets
        self.cseq = 0                               # Anzahl fertiger grober Buckets
        self._pend = np.empty((0, 3), dtype=np.float32)   # feine Buckets ab cseq*coarse
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
        t_last = now - len(self._rem) / self.rate     # Rest liegt zeitlich nach dem letzten Bucket
        coarse = self._coarsen(self.seq + skipped, mn, mx, av, t_last)
        with self._cond:
            start = (self.seq + skipped) % self.size
            idx = (start + np.arange(k)) % self.size
            self._min[idx] = mn
            self._max[idx] = mx
            self._avg[idx] = av
            self.seq += skipped + k
            self.t_last = t_last
            if coarse is not None:
                cmn, cmx, cav, ct = coarse
                cidx = (self.cseq + np.arange(len(ct))) % self.csize
                self._cmin[cidx] = cmn
                self._cmax[cidx] = cmx
                self._cavg[cidx] = cav
                self._ct[cidx] = ct
                self.cseq += len(ct)
            self._cond.notify_all()

    def _coarsen(self, a, mn, mx, av, t_last):
        """Feine Buckets ab Sequenz a zu groben zusammenfassen. Gibt fertige grobe Buckets
        (min, max, mittel, t_ende) oder None. Nur aus feed() aufrufen (ein Schreiber)."""
        R = self.coarse
        new = np.column_stack((mn, mx, av)).astype(np.float32)
        gap = a - (self.cseq * R + len(self._pend))
        parts = [self._pend]
        if gap > 0:                                 # übersprungene feine Buckets: Lücke
            parts.append(np.full((gap, 3), np.nan, dtype=np.float32))
        parts.append(new)
        x = np.concatenate(parts)
        m = len(x) // R
        self._pend = x[m * R:]
        if m == 0:
            return None
        g = x[: m * R].reshape(m, R, 3)
        if gap > 0:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                cmn, cmx, cav = np.nanmin(g[:, :, 0], 1), np.nanmax(g[:, :, 1], 1), np.nanmean(g[:, :, 2], 1)
        else:
            cmn, cmx, cav = g[:, :, 0].min(1), g[:, :, 1].max(1), g[:, :, 2].mean(1)
        fine_end = a + len(new)
        ends = (self.cseq + 1 + np.arange(m)) * R
        ct = t_last - (fine_end - ends) * self.bucket_s
        if m > self.csize:                          # nur bei riesigen Blöcken
            cmn, cmx, cav, ct = cmn[-self.csize:], cmx[-self.csize:], cav[-self.csize:], ct[-self.csize:]
            with self._lock:
                self.cseq += m - self.csize
        return cmn, cmx, cav, ct

    def history(self, von, bis):
        """Grobe Buckets [von, bis) als (von, bis, t_ende, min, max, mittel), auf das
        Vorhandene begrenzt."""
        with self._lock:
            von = max(int(von), self.cseq - self.csize, 0)
            bis = min(int(bis), self.cseq)
            n = max(0, bis - von)
            idx = (von + np.arange(n)) % self.csize
            return (von, von + n, self._ct[idx].copy(), self._cmin[idx].copy(),
                    self._cmax[idx].copy(), self._cavg[idx].copy())

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
                 wake_index=None, data_dir=None, projects=None, token=""):
        self.feed = feed
        self.data_dir = data_dir
        self.projects = projects
        self.token = token or ""
        self.wake_index = wake_index      # fest vorgegeben (Tests), sonst aus dem Ordner
        self._analysers = {}
        self._wake_workers = {}
        self._workers_lock = threading.Lock()
        self.summaries = Summaries()
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
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    an = server.analyser(q.get("projekt"))
                    if not an:
                        server._json(self, {"error": "kein Datenordner"})
                        return
                    server._json(self, an.run(q))
                elif url.path == "/api/projekte":
                    if not server.projects:
                        server._json(self, {"error": "keine Projektverwaltung"})
                        return
                    server._json(self, server._project_list())
                elif url.path == "/api/wake":
                    q = {k: v[0] for k, v in parse_qs(url.query).items()}
                    ww = server.wake_worker(q.get("projekt"))
                    if not ww:
                        server._json(self, {"error": "kein Datenordner"})
                        return
                    req = {"file": q.get("file", "")}
                    for k in ("a", "b", "n"):
                        if q.get(k, "").lstrip("-").isdigit():
                            req[k] = int(q[k])
                    if q.get("meta"):
                        req["meta"] = 1
                    server._json(self, ww.request(req))
                elif url.path == "/stream":
                    q = parse_qs(url.query)
                    try:
                        back = float(q.get("s", ["10"])[0])
                    except ValueError:
                        back = 10.0
                    self.close_connection = True   # Stream belegt die Verbindung ganz
                    server._stream(self, back)
                elif url.path == "/verlauf":
                    q = parse_qs(url.query)
                    try:
                        von = int(q.get("von", ["0"])[0])
                        bis = int(q.get("bis", [str(feed.cseq)])[0])
                    except ValueError:
                        self.send_error(400)
                        return
                    server._history(self, von, bis)
                elif url.path == "/wakes":
                    q = parse_qs(url.query)
                    try:
                        n = max(1, min(500, int(q.get("n", ["50"])[0])))
                    except ValueError:
                        n = 50
                    wi = server.wake_index_for(q.get("projekt", [None])[0])
                    rows = read_wakes(wi, n) if wi else []
                    server._json(self, {"wakes": rows, "available": bool(wi)})
                elif url.path == "/info":
                    body = json.dumps(dict(server.info, bucket_s=feed.bucket_s,
                                           history_s=feed.size * feed.bucket_s,
                                           coarse=feed.coarse, coarse_size=feed.csize,
                                           long_s=feed.csize * feed.coarse_s,
                                           clients=server.clients)).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def do_POST(self):
                url = urlparse(self.path)
                if url.path != "/api/projekte" or not server.projects:
                    self.send_error(404)
                    return
                server._post_projects(self)

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

    # ---------------------------------------------------------------- Projekte
    def _dir(self, slug=None):
        """Datenordner für eine Anfrage: angegebenes oder aktives Projekt, ohne
        Projektverwaltung der feste data_dir. ValueError bei unbekanntem Projekt."""
        if self.projects:
            return self.projects.dir_of(slug or self.projects.active)
        return self.data_dir

    def analyser(self, slug=None):
        try:
            d = self._dir(slug)
        except ValueError:
            return None
        if not d:
            return None
        with self._workers_lock:
            if d not in self._analysers:
                self._analysers[d] = Analyser(d)
            return self._analysers[d]

    def wake_worker(self, slug=None):
        """Ein Nebenprozess pro Ordner, höchstens zwei gleichzeitig."""
        try:
            d = self._dir(slug)
        except ValueError:
            return None
        if not d:
            return None
        with self._workers_lock:
            w = self._wake_workers.pop(d, None) or WakeWorker(d)
            self._wake_workers[d] = w          # ans Ende: zuletzt benutzt
            while len(self._wake_workers) > 2:
                old = next(iter(self._wake_workers))
                self._wake_workers.pop(old).close()
            return w

    def wake_index_for(self, slug=None):
        if self.wake_index and not slug:
            return self.wake_index
        try:
            d = self._dir(slug)
        except ValueError:
            return None
        return os.path.join(d, "wake", "wake_index.csv") if d else None

    def _project_list(self):
        p = self.projects
        out = []
        for m in p.list():
            if m["aktiv"]:
                with self.feed._lock:
                    st = dict(self.feed.status)
                m["live"] = st if st.get("projekt_slug", m["slug"]) == m["slug"] else {}
            else:
                m["zusammenfassung"] = self.summaries.get(p.dir_of(m["slug"]), m["settings"])
            out.append(m)
        return {"aktiv": p.active, "projekte": out, "vorgaben": p.defaults,
                "fest": p.fixed, "schutz": bool(self.token)}

    def _post_projects(self, h):
        h.close_connection = True        # Rumpf wird bei Fehlern evtl. nicht gelesen
        if self.token:
            given = h.headers.get("X-Token", "")
            if not hmac.compare_digest(given.encode(), self.token.encode()):
                self._json(h, {"error": "Passwort fehlt oder falsch", "token": True}, 403)
                return
        # Nur JSON: ein einfaches Formular fremder Seiten kann so nichts auslösen
        if not h.headers.get("Content-Type", "").startswith("application/json"):
            self._json(h, {"error": "nur application/json"}, 415)
            return
        try:
            n = int(h.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        if not 0 < n <= 65536:
            self._json(h, {"error": "ungültige Länge"}, 400)
            return
        try:
            q = json.loads(h.rfile.read(n))
            if not isinstance(q, dict):
                raise ValueError("Objekt erwartet")
            act, p = q.get("aktion"), self.projects
            if act == "neu":
                slug = p.create(q.get("name", ""), q.get("board", ""), q.get("notiz", ""),
                                q.get("settings") or {})
            elif act == "aktivieren":
                slug = p.activate(str(q.get("slug", "")))
            elif act == "aendern":
                slug = p.update(str(q.get("slug", "")), name=q.get("name"),
                                board=q.get("board"), notiz=q.get("notiz"),
                                settings=q.get("settings"))
            else:
                raise ValueError("unbekannte Aktion")
        except (ValueError, TypeError) as e:
            self._json(h, {"error": str(e)}, 400)
            return
        except OSError as e:
            self._json(h, {"error": f"Speichern fehlgeschlagen: {e}"}, 500)
            return
        self._json(h, dict(self._project_list(), ok=True, slug=slug))

    def _json(self, h, obj, code=200):
        body = json.dumps(obj).encode()
        h.send_response(code)
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

    def _history(self, h, von, bis):
        """Grobe Stufe binär: n × float64 (Zeit am Ende), dann n × 3 float32 (min, max, mittel)."""
        von, bis, t, mn, mx, av = self.feed.history(von, bis)
        body = t.astype("<f8").tobytes() + np.column_stack((mn, mx, av)).astype("<f4").tobytes()
        h.send_response(200)
        h.send_header("Content-Type", "application/octet-stream")
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Cache-Control", "no-cache")
        h.send_header("X-Von", str(von))
        h.send_header("X-Bis", str(bis))
        h.send_header("Access-Control-Expose-Headers", "X-Von, X-Bis")
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
        with self._workers_lock:
            for w in self._wake_workers.values():
                w.close()
            self._wake_workers.clear()
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
    Ohne data_dir: Vorgeschichte (48 h CSV) wird erzeugt und wie beim ersten Start in ein
    Projekt übernommen, Wachphasen mit WakeRecorder aufgenommen, Projektwechsel über die
    Seite /projekte möglich. Mit data_dir: vorhandene Daten (z. B. vom Pi kopiert) nur
    anzeigen (mit projekte.json samt Projektverwaltung)."""
    import tempfile
    from datetime import datetime, timezone
    from projects import STATE, ProjectStore
    from wake_capture import WakeRecorder
    rng = np.random.default_rng(0)
    feed = LiveFeed()
    own = data_dir is None
    defaults = {"battery_mAh": 3000.0, "battery_usable": 0.8, "quiet_max_uA": 5000.0,
                "redraw_uAh": 120.0, "wake_capture": True, "wake_trigger_uA": 5000.0,
                "wake_trigger_ms": 100.0, "wake_pre_s": 1.0, "wake_post_s": 1.0,
                "wake_max_s": 120.0, "wake_max_mb": 2000.0}
    store = None
    if own:
        data_dir = tempfile.mkdtemp(prefix="ppk2demo")
        print("Erzeuge 48 h Vorgeschichte ...", flush=True)
        _demo_history(data_dir)
    if own or os.path.exists(os.path.join(data_dir, STATE)):
        store = ProjectStore(data_dir, defaults, fixed={"mode": "demo", "voltage_mV": 3300})

    def make_rec(d, st):
        if not own or not st["wake_capture"]:
            return None
        return WakeRecorder(d, trigger_ua=st["wake_trigger_uA"], trigger_ms=st["wake_trigger_ms"],
                            pre_s=st["wake_pre_s"], post_s=st["wake_post_s"],
                            max_s=st["wake_max_s"], max_mb=st["wake_max_mb"], min_free_mb=0,
                            log=lambda s: None)

    gen, cur = store.snapshot() if store else (0, {"slug": "", "name": "", "dir": data_dir,
                                                   "settings": defaults})
    rec = make_rec(cur["dir"], cur["settings"])
    LiveServer(feed, port, host="127.0.0.1", info={"mode": "demo"}, data_dir=data_dir,
               projects=store, token=os.getenv("ADMIN_TOKEN", ""))
    print(f"Demo: http://127.0.0.1:{port}/  Auswertung: http://127.0.0.1:{port}/auswertung"
          f"  (Daten in {data_dir}, Strg+C beendet)", flush=True)
    rate, n, t0 = feed.rate, 0, time.time()
    t0r = t0                        # Start der Zählung (nach Projektwechsel neu)
    charge, last_stat, buf = 0.0, t0, []
    csv_f = None
    while True:
        time.sleep(0.1)
        if store and store.gen != gen:
            gen, new = store.snapshot()
            if rec and (new["slug"] != cur["slug"] or new["settings"] != cur["settings"]):
                rec.close()
                rec = None
            if new["slug"] != cur["slug"]:
                charge, t0r = 0.0, time.time()
            cur = new
            rec = rec or make_rec(cur["dir"], cur["settings"])
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
                path = os.path.join(cur["dir"], f"ppk2_{day:%Y-%m-%d}.csv")
                new = not os.path.exists(path)
                with open(path, "a", newline="") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(["timestamp_utc", "mean_uA", "min_uA", "max_uA", "samples",
                                    "charge_uAh_total", "median_uA"])
                    w.writerow([day.isoformat(timespec="seconds"), round(float(a.mean()), 3),
                                round(float(a.min()), 3), round(float(a.max()), 3), len(a),
                                round(charge, 4), round(float(np.median(a)), 3)])
            run = now - t0r
            avg = charge * 3600 / max(run, 1)
            cs = cur["settings"]
            st = {"charge_uAh": round(charge, 3), "runtime_h": run / 3600, "avg_total_uA": avg,
                  "quiet_avg_uA": 150.0 * 1.06,
                  "est_days": cs["battery_mAh"] * 1000 * cs["battery_usable"] / avg / 24,
                  "samples": len(a), "interval_s": round(now - last_stat, 3),
                  "quiet_window_s": 300, "battery_mAh": cs["battery_mAh"],
                  "battery_usable": cs["battery_usable"], "quiet_max_uA": cs["quiet_max_uA"],
                  "wake_capture": bool(rec), "wake_trigger_uA": cs["wake_trigger_uA"],
                  "wake_trigger_ms": cs["wake_trigger_ms"], "projekt_slug": cur["slug"]}
            if cur["name"]:
                st["projekt"] = cur["name"]
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
