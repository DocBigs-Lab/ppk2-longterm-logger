# SPDX-License-Identifier: GPL-2.0-only
"""Live-Ansicht: Verdichtung, Ringpuffer, HTTP-Server und Stream, ohne Hardware."""
import json
import os
import sys
import time
import unittest
import urllib.parse
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from live_view import LiveFeed, LiveServer  # noqa: E402


def read_events(url, n, timeout=5.0):
    """Die ersten n Daten-Ereignisse eines SSE-Streams lesen."""
    out = []
    with urllib.request.urlopen(url, timeout=timeout) as r:
        t_end = time.time() + timeout
        while len(out) < n and time.time() < t_end:
            line = r.readline().decode()
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


class FeedTest(unittest.TestCase):
    def test_buckets_unabhaengig_von_blockgroesse(self):
        x = np.random.default_rng(1).uniform(0, 1000, 100_007)
        ref = x[:100_000].reshape(-1, 100)
        f = LiveFeed(history_s=10)
        pos, sizes = 0, [1, 99, 100, 101, 3333, 40_000]
        i = 0
        while pos < len(x):
            n = sizes[i % len(sizes)]
            f.feed(x[pos:pos + n], 0.0)
            pos += n
            i += 1
        start, end, _, mn, mx, av = f.since(0)
        self.assertEqual((start, end), (0, 1000))
        np.testing.assert_allclose(mn, ref.min(axis=1), rtol=1e-6)
        np.testing.assert_allclose(mx, ref.max(axis=1), rtol=1e-6)
        np.testing.assert_allclose(av, ref.mean(axis=1), rtol=1e-6)

    def test_ringpuffer_ueberlauf(self):
        f = LiveFeed(history_s=1)            # 1000 Buckets
        for k in range(25):                  # 2,5 s in 0,1-s-Blöcken
            f.feed(np.full(10_000, float(k)), k * 0.1)
        start, end, _, mn, _, _ = f.since(0)
        self.assertEqual(end, 2500)
        self.assertEqual(end - start, 1000)
        self.assertEqual(float(mn[0]), 15.0)
        self.assertEqual(float(mn[-1]), 24.0)
        # Riesenblock größer als der Puffer
        f.feed(np.arange(300_000, dtype=float), 9.0)
        start, end, _, mn, _, _ = f.since(0)
        self.assertEqual(end, 5500)
        self.assertEqual(float(mn[-1]), 299_900.0)

    def test_rechenzeit(self):
        f = LiveFeed()
        x = np.random.default_rng(2).uniform(0, 1e4, 10_000)
        t = time.perf_counter()
        for _ in range(100):                 # 10 s Messung
            f.feed(x, 0.0)
        per_s = (time.perf_counter() - t) / 10
        print(f"\n  Live-Feed: {per_s * 1000:.2f} ms pro Sekunde Messung")
        self.assertLess(per_s, 0.02)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.feed = LiveFeed(history_s=5)
        self.srv = LiveServer(self.feed, 0, host="127.0.0.1", interval=0.02)
        self.base = f"http://127.0.0.1:{self.srv.port}"

    def tearDown(self):
        self.srv.close()

    def test_seite_und_info(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as r:
            self.assertIn(b"PPK2 Live", r.read())
        with urllib.request.urlopen(self.base + "/info", timeout=5) as r:
            info = json.loads(r.read())
        self.assertAlmostEqual(info["bucket_s"], 0.001)

    def test_stream_mit_vorlauf_und_neuen_daten(self):
        self.feed.feed(np.full(100_000, 150.0), 100.0)      # 1 s Vorlauf
        got = read_events(self.base + "/stream?s=0.5", 1)
        self.assertEqual(len(got), 1)
        m = got[0]
        self.assertEqual(m["end"], 1000)
        self.assertEqual(m["end"] - m["start"], 500)
        self.assertAlmostEqual(m["avg"][0], 150.0)
        self.assertAlmostEqual(m["t"], 100.0)

        # neue Daten kommen nach
        out = []
        import threading
        th = threading.Thread(target=lambda: out.extend(read_events(self.base + "/stream?s=0", 2)))
        th.start()
        time.sleep(0.2)
        self.feed.feed(np.full(10_000, 30_000.0), 100.1)
        th.join(5)
        datas = [m for m in out if m["end"] > m["start"]]
        self.assertTrue(datas)
        self.assertAlmostEqual(datas[-1]["max"][-1], 30_000.0)

    def test_status_im_stream(self):
        self.feed.feed(np.full(1000, 150.0), 1.0)
        self.feed.set_status({"quiet_avg_uA": 147.0, "wake_count": 3})
        with urllib.request.urlopen(self.base + "/stream?s=0", timeout=5) as r:
            ev, t_end = None, time.time() + 5
            while time.time() < t_end:
                line = r.readline().decode()
                if line.startswith("event: status"):
                    ev = json.loads(r.readline().decode()[6:])
                    break
        self.assertEqual(ev, {"quiet_avg_uA": 147.0, "wake_count": 3})

    def test_wachphasen_liste(self):
        import csv
        import tempfile
        from wake_capture import INDEX_HEADER
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "wake_index.csv")
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(INDEX_HEADER)
                for k in range(30):
                    w.writerow([f"2026-11-11T{k:02d}:00:00.000+00:00", f"2026-11-11T{k:02d}:00:02.000+00:00",
                                34.0, 30.0, 2.0, 200.0 + k, 190.0 + k, 21000.0, 140000.0, 110000.0,
                                3400000, 0, f"x{k}.npz", "ok"])
            self.srv.wake_index = path
            with urllib.request.urlopen(self.base + "/wakes?n=5", timeout=5) as r:
                j = json.loads(r.read())
        self.assertEqual(len(j["wakes"]), 5)
        self.assertEqual(j["wakes"][0]["file"], "x29.npz")          # neueste zuerst
        self.assertEqual(j["wakes"][0]["active_uAh"], 219.0)
        self.srv.wake_index = os.path.join(d, "fehlt.csv")
        with urllib.request.urlopen(self.base + "/wakes", timeout=5) as r:
            self.assertEqual(json.loads(r.read())["wakes"], [])

    def test_zu_viele_verbindungen(self):
        self.srv.max_clients = 0
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/stream", timeout=5)
        self.assertEqual(cm.exception.code, 503)


class AuswertungTest(unittest.TestCase):
    """/auswertung, /api/analyse (analyse.py als Prozess), /api/wake (wake_view.py --serve)."""

    @classmethod
    def setUpClass(cls):
        import csv
        import tempfile
        from datetime import datetime, timedelta, timezone
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        t0 = datetime(2026, 11, 11, 6, 30, tzinfo=timezone.utc)
        with open(os.path.join(d, "ppk2_2026-11-11.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp_utc", "mean_uA", "min_uA", "max_uA", "samples",
                        "charge_uAh_total", "median_uA"])
            for k in range(3 * 3600):
                t = t0 + timedelta(seconds=k)
                m = 30000.0 if t.minute == 0 and t.second < 30 else 150.0
                w.writerow([t.isoformat(timespec="seconds"), m, 1, m, 100000, 0, m])
        os.makedirs(os.path.join(d, "wake", "2026-11-11"))
        cls.arr = np.random.default_rng(4).uniform(100, 60000, 250_000).astype(np.float32)
        meta = {"format": 2, "rate": 100000, "rate_eff": 100000.0, "pre_samples": 20000,
                "active_samples": 200000, "start_epoch": 1.0}
        np.savez_compressed(os.path.join(d, "wake", "2026-11-11", "w.npz"), uA=cls.arr,
                            meta=np.array(json.dumps(meta)))
        cls.feed = LiveFeed(history_s=5)
        cls.srv = LiveServer(cls.feed, 0, host="127.0.0.1", data_dir=d)
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=60) as r:
            return r.read()

    def test_seite(self):
        self.assertIn(b"PPK2 Auswertung", self.get("/auswertung"))

    def test_analyse(self):
        j = json.loads(self.get("/api/analyse?hours=48&tz=1"))
        self.assertNotIn("error", j)
        self.assertEqual(len(j["wakes"]), 3)                  # 07:00, 08:00, 09:00 UTC
        self.assertAlmostEqual(j["quiet"]["mean_uA"], 150.0, delta=0.5)
        self.assertAlmostEqual(j["measured_h"], 3.0, delta=0.01)
        self.assertEqual(len(j["hourly"]), 4)
        # ungültige Werte werden abgewiesen, ohne Prozess zu starten
        self.assertIn("error", json.loads(self.get("/api/analyse?hours=abc")))
        self.assertIn("error", json.loads(self.get("/api/analyse?von=2026-11-11;rm")))

    def test_wake_ganz_und_ausschnitt(self):
        j = json.loads(self.get("/api/wake?file=2026-11-11/w.npz&n=1000&meta=1"))
        self.assertNotIn("error", j)
        self.assertEqual(j["total"], len(self.arr))
        self.assertEqual(j["per"], 250)
        self.assertEqual(len(j["mean"]), 1000)
        self.assertEqual(j["meta"]["pre_samples"], 20000)
        a, b = 12_345, 67_890
        j = json.loads(self.get(f"/api/wake?file=2026-11-11/w.npz&a={a}&b={b}&n=500"))
        seg = self.arr[a:b].astype(np.float64)
        self.assertAlmostEqual(j["seg_uAh"], seg.sum() / 1e5 / 3600, places=6)
        self.assertAlmostEqual(j["seg_max_uA"], float(seg.max()), places=1)
        j = json.loads(self.get("/api/wake?file=2026-11-11/w.npz&a=1000&b=1300&n=500"))
        self.assertEqual(j["per"], 1)                          # Rohwerte
        np.testing.assert_allclose(j["mean"], self.arr[1000:1300], rtol=1e-4)

    def test_wake_pfade(self):
        for bad in ("../../etc/passwd", "../wake/../x.npz", "/etc/hosts", "2026-11-11/fehlt.npz"):
            j = json.loads(self.get("/api/wake?file=" + urllib.parse.quote(bad)))
            self.assertIn("error", j, bad)


if __name__ == "__main__":
    unittest.main()
