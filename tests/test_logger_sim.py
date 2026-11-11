# SPDX-License-Identifier: GPL-2.0-only
"""Smoke-Test des Loggers mit einem Fake-PPK2 (Echtzeit, ca. 8 s).

Der Fake liefert float32-µA-Werte als "Rohbytes", ein Fake-Dekoder wandelt sie zurück.
Geprüft werden Hauptschleife, CSV, MQTT-Payload, Discovery-Liste und Wachphasen-Aufnahme.
Der echte Dekoder (ppk2_fast.py) ist nicht Teil dieses Tests.
"""
import csv
import glob
import importlib
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

RATE = 100_000
WAKE_AT, WAKE_LEN, WAKE_UA, BASE_UA = 3.0, 1.5, 30_000.0, 150.0


class FakePPK2:
    """Nachbildung der von ppk2_logger genutzten PPK2_MP-Schnittstelle."""

    def __init__(self, port):
        self.port = port
        self.t0 = None
        self.sent = 0
        self.calls = []

    @staticmethod
    def list_devices():
        return ["/dev/ttyACM1", "/dev/ttyACM0"]

    def get_modifiers(self):
        return True

    def use_ampere_meter(self):
        self.calls.append("ampere")

    def use_source_meter(self):
        self.calls.append("source")

    def set_source_voltage(self, mv):
        self.calls.append(f"voltage {mv}")

    def toggle_DUT_power(self, state):
        self.calls.append(f"power {state}")

    def start_measuring(self):
        self.t0 = time.time()

    def stop_measuring(self):
        self.calls.append("stop")

    def get_data(self):
        if self.t0 is None:
            return b""
        due = int((time.time() - self.t0) * RATE)
        n = due - self.sent
        if n <= 0:
            return b""
        t = (self.sent + np.arange(n)) / RATE
        ua = np.where((t >= WAKE_AT) & (t < WAKE_AT + WAKE_LEN), WAKE_UA, BASE_UA)
        self.sent = due
        return ua.astype(np.float32).tobytes()


class FakeDecoder:
    def __init__(self, ppk2):
        pass

    def decode(self, raw):
        return np.frombuffer(raw, dtype=np.float32).astype(np.float64)


class FakeMQTT:
    def __init__(self):
        self.msgs = []

    def publish(self, topic, payload, retain=False):
        self.msgs.append((topic, json.loads(payload)))


def load_logger(env):
    if "ppk2_api.ppk2_api" not in sys.modules:
        try:
            importlib.import_module("ppk2_api.ppk2_api")
        except ImportError:   # Bibliothek nicht installiert: Platzhalter reicht hier
            pkg, mod = types.ModuleType("ppk2_api"), types.ModuleType("ppk2_api.ppk2_api")
            mod.PPK2_API = mod.PPK2_MP = FakePPK2
            sys.modules.update({"ppk2_api": pkg, "ppk2_api.ppk2_api": mod})
    os.environ.update(env)
    sys.modules.pop("ppk2_logger", None)
    lg = importlib.import_module("ppk2_logger")
    lg.PPK2_API = lg.PPK2_MP = FakePPK2
    lg.FastDecoder = FakeDecoder
    return lg


class LoggerSimTest(unittest.TestCase):
    def test_logger_mit_wachphase(self):
        with tempfile.TemporaryDirectory() as d:
            lg = load_logger({
                "OUT_DIR": d, "PPK2_MODE": "ampere", "PPK2_PORT": "", "LOG_INTERVAL_S": "1",
                "WAKE_CAPTURE": "1", "WAKE_PRE_S": "1", "WAKE_POST_S": "1",
                "WAKE_MIN_FREE_MB": "0", "MQTT_TOPIC": "test/ppk2",
            })
            mq = FakeMQTT()
            th = threading.Thread(target=lg.measure, args=(mq,))
            th.start()
            time.sleep(8)
            lg.running = False
            th.join(20)
            self.assertFalse(th.is_alive())

            # CSV: Header unverändert, ca. 7 Zeilen, Ladung plausibel
            (path,) = glob.glob(os.path.join(d, "ppk2_*.csv"))
            with open(path, newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], lg.CSV_HEADER)
            self.assertGreaterEqual(len(rows) - 1, 6)
            charge = float(rows[-1][5])
            expect = WAKE_UA * WAKE_LEN / 3600.0
            self.assertAlmostEqual(charge, expect, delta=expect * 0.15)

            # Wachphase gespeichert
            files = glob.glob(os.path.join(d, "wake", "*", "*.npz"))
            self.assertEqual(len(files), 1)
            with np.load(files[0], allow_pickle=False) as z:
                meta = json.loads(str(z["meta"]))
                n = len(z["uA"])
            # Standardfenster 100 ms: Trigger ca. 16 ms nach Beginn, Ende ca. 85 ms danach
            self.assertEqual(meta["trigger_ms"], 100)
            self.assertAlmostEqual(meta["active_s"], WAKE_LEN, delta=0.15)
            self.assertAlmostEqual(meta["active_uAh"], expect, delta=expect * 0.03)
            self.assertAlmostEqual(n / RATE, WAKE_LEN + 2.0, delta=0.15)
            self.assertAlmostEqual(meta["rate_eff"], RATE, delta=RATE * 0.02)
            self.assertEqual(meta["mode"], "ampere")
            print(f"\n  Ratenschätzung: {meta['rate_est']} Samples/s")

            # MQTT-Payload enthält die neuen Werte, alte Schlüssel bleiben
            last = mq.msgs[-1][1]
            self.assertEqual(mq.msgs[-1][0], "test/ppk2")
            self.assertEqual(last["wake_count"], 1)
            self.assertAlmostEqual(last["wake_last_uAh"], expect, delta=expect * 0.03)
            for k in ("mean_uA", "charge_uAh", "runtime_h", "avg_total_uA"):
                self.assertIn(k, last)

            # Discovery: bisherige Sensoren unverändert, neue nur mit WAKE_CAPTURE
            keys = [s[0] for s in lg.ha_sensors()]
            self.assertEqual(keys[:9], ["mean_uA", "quiet_avg_uA", "median_uA", "min_uA",
                                        "max_uA", "charge_uAh", "avg_total_uA",
                                        "runtime_h", "est_days"])
            self.assertEqual(keys[9:], ["wake_last_uAh", "wake_last_s", "wake_count"])
            lg.WAKE_CAPTURE = False
            self.assertEqual(len(lg.ha_sensors()), 9)

    def test_recorder_fehler_stoppt_messung_nicht(self):
        with tempfile.TemporaryDirectory() as d:
            lg = load_logger({"OUT_DIR": d, "WAKE_CAPTURE": "1", "LOG_INTERVAL_S": "1",
                              "WAKE_MIN_FREE_MB": "0"})

            class Broken:
                count, last = 0, None

                def feed(self, *a):
                    raise RuntimeError("Absicht")

                def close(self, **kw):
                    pass

            lg.make_recorder = lambda: Broken()
            th = threading.Thread(target=lg.measure, args=(None,))
            th.start()
            time.sleep(3)
            lg.running = False
            th.join(10)
            (path,) = glob.glob(os.path.join(d, "ppk2_*.csv"))
            with open(path) as f:
                self.assertGreaterEqual(len(f.readlines()) - 1, 2)

    def test_live_ansicht(self):
        from live_view import LiveFeed
        with tempfile.TemporaryDirectory() as d:
            lg = load_logger({"OUT_DIR": d, "WAKE_CAPTURE": "0", "LOG_INTERVAL_S": "1",
                              "LIVE_PORT": ""})
            self.assertEqual(lg.make_live(), (None, None))     # Standard: aus
            feed = LiveFeed(history_s=10)
            th = threading.Thread(target=lg.measure, args=(None, feed))
            th.start()
            time.sleep(5)
            lg.running = False
            th.join(10)
            start, end, _, mn, mx, av = feed.since(0)
            self.assertGreater(end, 4000)                       # > 4 s in 1-ms-Buckets
            # Wachphase der Fake-Quelle (3,0 bis 4,5 s) in der Hülle sichtbar
            self.assertAlmostEqual(float(mx.max()), WAKE_UA, delta=1)
            self.assertAlmostEqual(float(np.median(av)), BASE_UA, delta=1)
            # Kennzahlen des Loggers kommen in der Live-Ansicht an
            st = feed.status
            for key in ("charge_uAh", "samples", "interval_s", "runtime_h", "est_days"):
                self.assertIn(key, st)
            self.assertFalse(st["wake_capture"])
            self.assertGreater(st["samples"] / st["interval_s"], 50_000)

    def test_live_fehler_stoppt_messung_nicht(self):
        with tempfile.TemporaryDirectory() as d:
            lg = load_logger({"OUT_DIR": d, "WAKE_CAPTURE": "0", "LOG_INTERVAL_S": "1"})

            class Broken:
                def feed(self, *a):
                    raise RuntimeError("Absicht")

            th = threading.Thread(target=lg.measure, args=(None, Broken()))
            th.start()
            time.sleep(3)
            lg.running = False
            th.join(10)
            (path,) = glob.glob(os.path.join(d, "ppk2_*.csv"))
            with open(path) as f:
                self.assertGreaterEqual(len(f.readlines()) - 1, 2)


if __name__ == "__main__":
    unittest.main()
