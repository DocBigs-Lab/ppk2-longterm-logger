# SPDX-License-Identifier: GPL-2.0-only
"""Tests für wake_capture.WakeRecorder mit synthetischen Signalen.
Aufruf im Repo-Ordner: python3 -m unittest discover tests"""
import csv
import json
import os
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sim import RATE, WAKE_UAH, feed, pulsed_sleep, segment, wake_phase  # noqa: E402
from wake_capture import INDEX_HEADER, WakeRecorder  # noqa: E402

T0 = 1_800_000_000.0


def files(d):
    out = []
    for root, _, names in os.walk(os.path.join(d, "wake")):
        out += [os.path.join(root, n) for n in names if n.endswith(".npz")]
    return sorted(out, key=os.path.basename)


def index(d):
    with open(os.path.join(d, "wake", "wake_index.csv"), newline="") as f:
        return list(csv.DictReader(f))


def load(p):
    with np.load(p, allow_pickle=False) as z:
        return z["uA"], json.loads(str(z["meta"]))


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def rec(self, **kw):
        kw.setdefault("log", lambda s: None)
        kw.setdefault("min_free_mb", 0)
        kw.setdefault("trigger_ms", 1.0)   # exakte Zeitpunkte; 100 ms siehe Puls-Tests
        return WakeRecorder(self.d, **kw)

    def test_einzelne_wachphase(self):
        base = 150.0
        sig = np.concatenate((segment(10, base), wake_phase(), segment(10, base)))
        r = self.rec()
        feed(r, sig)
        r.close()
        fs = files(self.d)
        self.assertEqual(len(fs), 1)
        a, meta = load(fs[0])
        self.assertEqual(a.dtype, np.float32)
        self.assertEqual(meta["pre_samples"], 2 * RATE)
        # Vorlauf ist Ruhestrom, Trigger direkt danach
        self.assertLess(abs(a[:meta["pre_samples"]].mean() - base), 5)
        self.assertGreater(a[meta["pre_samples"]:meta["pre_samples"] + 100].mean(), 5000)
        # Einbruch von 0,5 s beendet die Aufnahme nicht: 25,5 s aktiv + 2 s Vor-/Nachlauf
        self.assertAlmostEqual(meta["active_s"], 25.5, delta=0.01)
        self.assertAlmostEqual(meta["duration_s"], 29.5, delta=0.01)
        self.assertAlmostEqual(meta["active_uAh"], WAKE_UAH, delta=WAKE_UAH * 0.01)
        # Zeitstempel: Trigger bei 10 s nach Start
        self.assertAlmostEqual(meta["trigger_epoch"], T0 + 10.0, delta=0.001)
        self.assertAlmostEqual(meta["start_epoch"], T0 + 8.0, delta=0.001)
        self.assertEqual(meta["truncated"], 0)
        rows = index(self.d)
        self.assertEqual(len(rows), 1)
        self.assertEqual(list(rows[0].keys()), INDEX_HEADER)
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(r.count, 1)
        self.assertAlmostEqual(r.last["active_uAh"], WAKE_UAH, delta=WAKE_UAH * 0.01)

    def test_puls_direkt_nach_start_loest_nicht_aus(self):
        # Erstes Sample ein 30-mA-Puls: das 100-ms-Fenster ist noch nicht voll
        x = np.full(300_000, 150.0, dtype=np.float32)
        x[:100] = 30_000.0
        rec = self.rec(trigger_ms=100.0)
        feed(rec, x)
        rec.close()
        self.assertEqual(rec.count, 0)

    def test_einzelner_spike_loest_nicht_aus(self):
        sig = segment(5, 150.0)
        sig[200_000] = 50_000.0          # 1 Sample 50 mA, 1-ms-Mittel bleibt bei ca. 650 µA
        sig[300_000:300_010] = 40_000.0  # 100 µs 40 mA -> 1-ms-Mittel ca. 4,1 mA
        r = self.rec()
        feed(r, sig)
        r.close()
        self.assertEqual(files(self.d), [])
        self.assertEqual(r.count, 0)

    def test_blockgroesse_egal(self):
        sig = np.concatenate((segment(3, 150.0), wake_phase(), segment(3, 150.0),
                              wake_phase(np.random.default_rng(7)), segment(3, 150.0)))
        results = []
        for jitter, chunk in ((False, 10_000), (True, 3_000), (False, len(sig))):
            with tempfile.TemporaryDirectory() as d:
                r = WakeRecorder(d, log=lambda s: None, min_free_mb=0)
                feed(r, sig, chunk=chunk, jitter=jitter)
                r.close()
                fs = sorted(files(d))
                results.append([load(p)[0] for p in fs])
        self.assertEqual(len(results[0]), 2)
        for other in results[1:]:
            self.assertEqual(len(other), 2)
            for a, b in zip(results[0], other):
                np.testing.assert_array_equal(a, b)

    def test_kurzer_vorlauf_am_anfang(self):
        sig = np.concatenate((segment(0.5, 150.0), wake_phase(), segment(3, 150.0)))
        r = self.rec()
        feed(r, sig)
        r.close()
        a, meta = load(files(self.d)[0])
        # Das 1-ms-Mittel überschreitet die Schwelle erst nach einigen Samples
        self.assertAlmostEqual(meta["pre_samples"], RATE // 2, delta=20)

    def test_hoechstdauer_und_sperre(self):
        # 40 s Dauerstrom (z. B. Laden): Aufnahme wird nach 30 s abgeschnitten, danach
        # keine neue Aufnahme, bis es ruhig ist. Dann eine normale Wachphase.
        sig = np.concatenate((segment(3, 150.0), segment(40, 20_000.0), segment(5, 150.0),
                              wake_phase(), segment(3, 150.0)))
        r = self.rec(max_s=30)
        feed(r, sig)
        r.close()
        fs = files(self.d)
        self.assertEqual(len(fs), 2)
        a1, m1 = load(fs[0])
        self.assertEqual(m1["truncated"], 1)
        self.assertAlmostEqual(m1["active_s"], 30.0, delta=0.001)
        self.assertEqual(len(a1), 32 * RATE)
        a2, m2 = load(fs[1])
        self.assertEqual(m2["truncated"], 0)
        self.assertAlmostEqual(m2["active_s"], 25.5, delta=0.01)

    def test_zwei_phasen_dicht_hintereinander(self):
        # Pause von 3 s: erste Aufnahme endet nach 2 s Nachlauf, zweite hat 1 s Vorlauf
        # aus dem Nachlauf der ersten plus 1 s neu
        sig = np.concatenate((segment(3, 150.0), segment(1, 30_000.0), segment(3, 150.0),
                              segment(1, 30_000.0), segment(3, 150.0)))
        r = self.rec()
        feed(r, sig)
        r.close()
        fs = files(self.d)
        self.assertEqual(len(fs), 2)
        _, m2 = load(fs[1])
        self.assertEqual(m2["pre_samples"], 2 * RATE)
        self.assertAlmostEqual(m2["trigger_epoch"], T0 + 7.0, delta=0.001)

    def test_speichergrenze(self):
        parts = [segment(3, 150.0)]
        for i in range(4):
            parts += [segment(2, 30_000.0, rng=np.random.default_rng(i)), segment(5, 150.0)]
        sig = np.concatenate(parts)
        # eine Datei hat ca. 6 s * 100 k * 4 B = 2,4 MB unkomprimiert
        r = self.rec(max_mb=4)
        feed(r, sig)
        r.close()
        fs = files(self.d)
        total = sum(os.path.getsize(p) for p in fs)
        self.assertLessEqual(total, 4e6)
        self.assertGreaterEqual(len(fs), 1)
        self.assertEqual(r.saved, 4)
        # die neueste Datei bleibt
        _, m = load(fs[-1])
        self.assertAlmostEqual(m["trigger_epoch"], T0 + 3 + 3 * 7, delta=0.001)
        self.assertEqual(len(index(self.d)), 4)

    def test_wenig_speicherplatz(self):
        sig = np.concatenate((segment(3, 150.0), segment(1, 30_000.0), segment(3, 150.0)))
        r = self.rec(min_free_mb=1e12)
        feed(r, sig)
        r.close()
        self.assertEqual(files(self.d), [])
        self.assertEqual(r.dropped, 1)
        self.assertTrue(index(self.d)[0]["status"].startswith("verworfen"))

    def test_schreiber_belegt(self):
        r = self.rec()
        orig = r._save

        def slow(*a):
            time.sleep(0.5)
            orig(*a)

        r._save = slow
        sig = np.concatenate((segment(3, 150.0), segment(1, 30_000.0), segment(3, 150.0),
                              segment(1, 30_000.0), segment(3, 150.0),
                              segment(1, 30_000.0), segment(3, 150.0)))
        feed(r, sig, chunk=len(sig), wait=False)   # alles auf einmal: drei Phasen fast gleichzeitig
        r.close()
        self.assertEqual(r.count, 3)
        self.assertGreaterEqual(r.dropped, 1)
        self.assertEqual(r.saved + r.dropped, 3)
        self.assertEqual(len(index(self.d)), 3)

    def test_abbruch_beim_beenden(self):
        sig = np.concatenate((segment(3, 150.0), segment(4, 30_000.0)))
        r = self.rec()
        feed(r, sig)
        r.close()
        fs = files(self.d)
        self.assertEqual(len(fs), 1)
        _, m = load(fs[0])
        self.assertTrue(m["aborted"])

    def test_gepulster_ruhestrom_100ms(self):
        # Wie auf dem Pi gemessen: Pulse im Ruhestrom dürfen nicht auslösen, die echte
        # Wachphase dazwischen wird vollständig aufgenommen
        sig = np.concatenate((pulsed_sleep(30), wake_phase(), pulsed_sleep(30)))
        r = self.rec(trigger_ms=100)
        feed(r, sig)
        r.close()
        fs = files(self.d)
        self.assertEqual(len(fs), 1)
        _, m = load(fs[0])
        self.assertEqual(m["truncated"], 0)
        self.assertAlmostEqual(m["active_s"], 25.5, delta=0.15)
        self.assertAlmostEqual(m["active_uAh"], WAKE_UAH, delta=WAKE_UAH * 0.01)
        self.assertAlmostEqual(m["trigger_epoch"], T0 + 30.0, delta=0.02)

    def test_gepulster_ruhestrom_1ms_loest_aus(self):
        # Gegenprobe: mit 1-ms-Fenster lösen die Pulse aus (das Verhalten vom 09.11.)
        r = self.rec(trigger_ms=1, max_s=10)
        feed(r, pulsed_sleep(30))
        r.close()
        self.assertGreaterEqual(r.count, 1)

    def test_effektive_rate(self):
        # Am PPK2 kommen ca. 88 000 Samples pro Wanduhr-Sekunde an: Dauer und Ladung
        # müssen mit dieser Rate gerechnet werden
        wall = 88_000
        sig = np.concatenate((segment(15, 150.0), wake_phase(), segment(5, 150.0)))
        r = self.rec()
        feed(r, sig, wall_rate=wall)
        r.close()
        _, m = load(files(self.d)[0])
        self.assertAlmostEqual(m["rate_eff"], wall, delta=wall * 0.001)
        self.assertEqual(m["rate"], RATE)
        scale = RATE / wall
        self.assertAlmostEqual(m["active_s"], 25.5 * scale, delta=0.02)
        self.assertAlmostEqual(m["active_uAh"], WAKE_UAH * scale, delta=WAKE_UAH * 0.01)
        self.assertAlmostEqual(m["trigger_epoch"], T0 + 15 * scale, delta=0.002)
        self.assertAlmostEqual(m["start_epoch"], m["trigger_epoch"] - 2 * scale, delta=0.002)

    def test_rechenzeit(self):
        # 10 s Ruhe in 0,1-s-Blöcken: Aufwand pro Sekunde Messung
        sig = segment(10, 150.0)
        r = self.rec()
        t = time.perf_counter()
        feed(r, sig)
        dt = time.perf_counter() - t
        r.close()
        print(f"\n  Rechenzeit Ruhe: {dt / 10 * 1000:.1f} ms pro Sekunde Messung")
        self.assertLess(dt / 10, 0.1)


if __name__ == "__main__":
    unittest.main()
