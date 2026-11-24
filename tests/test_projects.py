# SPDX-License-Identifier: GPL-2.0-only
"""Projekte: Ablage und Migration, Wechsel im laufenden Logger (Fake-PPK2), Web-API."""
import csv
import glob
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from projects import ProjectStore, check_settings, resolve_dir, slugify  # noqa: E402

DEFAULTS = {"battery_mAh": 3000.0, "battery_usable": 0.8, "quiet_max_uA": 5000.0,
            "redraw_uAh": 120.0, "wake_capture": False, "wake_trigger_uA": 5000.0,
            "wake_trigger_ms": 100.0, "wake_pre_s": 2.0, "wake_post_s": 2.0,
            "wake_max_s": 120.0, "wake_max_mb": 2000.0}
HEADER = "timestamp_utc,mean_uA,min_uA,max_uA,samples,charge_uAh_total,median_uA\n"


def quiet(*_):
    pass


class StoreTest(unittest.TestCase):
    def test_migration_bisheriger_daten(self):
        with tempfile.TemporaryDirectory() as d:
            for day in ("2026-11-10", "2026-11-11"):
                with open(os.path.join(d, f"ppk2_{day}.csv"), "w") as f:
                    f.write(HEADER + f"{day}T10:00:00+00:00,150,1,2,100000,0.1,150\n"
                             f"{day}T10:00:01+00:00,151,1,2,100000,0.2,150\n")
            os.makedirs(os.path.join(d, "wake", "2026-11-10"))
            open(os.path.join(d, "wake", "wake_index.csv"), "w").close()
            open(os.path.join(d, "roh_mp.bin"), "w").close()       # bleibt liegen
            st = ProjectStore(d, DEFAULTS, fixed={"mode": "ampere"}, log=quiet)
            slug = st.active
            pd = st.dir_of(slug)
            self.assertEqual(sorted(os.listdir(pd)), ["ppk2_2026-11-10.csv",
                                                      "ppk2_2026-11-11.csv", "projekt.json",
                                                      "wake"])
            self.assertTrue(os.path.exists(os.path.join(d, "roh_mp.bin")))
            self.assertEqual(glob.glob(os.path.join(d, "ppk2_*")), [])
            info = st.info(slug)
            self.assertEqual(info["name"], "Bisherige Messung")
            self.assertEqual(info["angelegt"], "2026-11-10T10:00:00+00:00")
            self.assertEqual(info["von"], "2026-11-10T10:00:00+00:00")
            self.assertEqual(info["bis"], "2026-11-11T10:00:01+00:00")
            self.assertEqual(info["fest"], {"mode": "ampere"})
            # Zweiter Start: nichts mehr zu verschieben, gleiches Projekt aktiv
            st2 = ProjectStore(d, DEFAULTS, log=quiet)
            self.assertEqual(st2.active, slug)
            self.assertEqual(len(st2.list()), 1)
            self.assertEqual(resolve_dir(d), pd)

    def test_leer_anlegen_wechseln_aendern(self):
        with tempfile.TemporaryDirectory() as d:
            st = ProjectStore(d, DEFAULTS, log=quiet)
            first = st.active
            self.assertEqual(st.get(first)["name"], "Projekt 1")
            g = st.gen
            s = st.create("Neues Board (nRF52)", board="nRF52840 DK",
                          settings={"battery_mAh": 220, "wake_capture": True})
            self.assertEqual(s, "neues-board-nrf52")
            self.assertEqual(st.active, s)
            self.assertEqual(st.gen, g + 1)
            self.assertIsNotNone(st.get(first)["archiviert"])
            self.assertIsNone(st.get(s)["archiviert"])
            gen, snap = st.snapshot()
            self.assertEqual(snap["settings"]["battery_mAh"], 220.0)
            self.assertEqual(snap["settings"]["quiet_max_uA"], 5000.0)   # aus Vorgabe
            self.assertTrue(snap["settings"]["wake_capture"])
            # Gleicher Name: eigener Ordner
            self.assertEqual(st.create("Neues Board (nRF52)", activate=False),
                             "neues-board-nrf52-2")
            self.assertEqual(st.active, s)
            # Ändern des aktiven Projekts zählt gen hoch, eines anderen nicht
            st.update(s, settings={"quiet_max_uA": 2000})
            self.assertEqual(st.gen, gen + 1)
            st.update(first, notiz="alt")
            self.assertEqual(st.gen, gen + 1)
            # Fortsetzen
            st.activate(first)
            self.assertEqual(st.active, first)
            self.assertIsNone(st.get(first)["archiviert"])
            self.assertIsNotNone(st.get(s)["archiviert"])
            # Nach Neustart gleicher Zustand
            st2 = ProjectStore(d, DEFAULTS, log=quiet)
            self.assertEqual(st2.active, first)
            self.assertEqual(st2.get(s)["settings"]["quiet_max_uA"], 2000.0)

    def test_pruefungen(self):
        with tempfile.TemporaryDirectory() as d:
            st = ProjectStore(d, DEFAULTS, log=quiet)
            for bad in ({"battery_usable": 1.5}, {"quiet_max_uA": "x"}, {"gibtsnicht": 1},
                        {"wake_max_mb": float("nan")}):
                with self.assertRaises(ValueError):
                    st.create("x", settings=bad)
            with self.assertRaises(ValueError):
                st.create("   ")
            with self.assertRaises(ValueError):
                st.activate("../etc")
            with self.assertRaises(ValueError):
                st.dir_of("gibt-es-nicht")
            self.assertEqual(len(st.list()), 1)
        self.assertEqual(slugify("Größe & Öl / Test"), "groesse-oel-test")
        self.assertEqual(slugify("!!!"), "projekt")
        self.assertEqual(check_settings({"wake_capture": "ja"}, DEFAULTS)["wake_capture"], True)


class LoggerWechselTest(unittest.TestCase):
    """Projektwechsel während der Messung: neue CSV im neuen Ordner, Zähler bei 0,
    das PPK2 wird dabei nicht angefasst (kein Stromschnitt)."""

    def test_wechsel_ohne_neustart(self):
        from test_logger_sim import BASE_UA, FakeMQTT, FakePPK2, load_logger
        with tempfile.TemporaryDirectory() as d:
            lg = load_logger({"OUT_DIR": d, "PPK2_MODE": "ampere", "LOG_INTERVAL_S": "1",
                              "WAKE_CAPTURE": "1", "WAKE_PRE_S": "1", "WAKE_POST_S": "1",
                              "WAKE_MIN_FREE_MB": "0", "MQTT_TOPIC": "test/ppk2"})
            devs = []

            class Spy(FakePPK2):
                def __init__(self, port):
                    super().__init__(port)
                    devs.append(self)

            lg.PPK2_MP = Spy
            st = ProjectStore(d, lg.env_settings(), log=quiet)
            old = st.active
            mq = FakeMQTT()
            th = threading.Thread(target=lg.measure, args=(mq, None, st))
            th.start()
            time.sleep(5.5)          # Wachphase der Fake-Quelle (3,0-4,5 s) im alten Projekt
            new = st.create("Board 2", settings={"wake_capture": False, "battery_mAh": 100})
            time.sleep(3.5)
            lg.running = False
            th.join(20)
            self.assertFalse(th.is_alive())

            self.assertEqual(len(devs), 1)
            self.assertEqual(devs[0].calls.count("power ON"), 1)
            self.assertEqual(devs[0].calls[-1], "stop")             # erst beim Beenden

            def rows(slug):
                (p,) = glob.glob(os.path.join(st.dir_of(slug), "ppk2_*.csv"))
                with open(p, newline="") as f:
                    return list(csv.reader(f))[1:]
            r_old, r_new = rows(old), rows(new)
            self.assertGreaterEqual(len(r_old), 4)
            self.assertGreaterEqual(len(r_new), 2)
            # Ladung im neuen Projekt beginnt bei 0: nur Ruhestrom seit dem Wechsel
            self.assertLess(float(r_new[-1][5]), BASE_UA * 5 / 3600)
            self.assertGreater(float(r_old[-1][5]), 10)             # mit Wachphase
            self.assertEqual(len(glob.glob(os.path.join(st.dir_of(old), "wake", "*", "*.npz"))), 1)
            self.assertFalse(os.path.exists(os.path.join(st.dir_of(new), "wake")))

            states = [m for t, m in mq.msgs if t == "test/ppk2"]
            self.assertEqual(states[0]["projekt"], "Projekt 1")
            self.assertEqual(states[-1]["projekt"], "Board 2")
            self.assertNotIn("wake_count", states[-1])
            # Laufzeit mit 100 mAh aus dem neuen Projekt
            avg = states[-1]["avg_total_uA"]
            self.assertAlmostEqual(states[-1]["est_days"], 100 * 1000 * 0.8 / avg / 24, delta=0.2)
            # Discovery: Wachphasen-Sensoren entfernt (leere retained-Nachricht)
            removed = [t for t, m in mq.msgs if m is None]
            self.assertIn("homeassistant/sensor/ppk2_wake_count/config", removed)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from live_view import LiveFeed, LiveServer
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = ProjectStore(cls.tmp.name, DEFAULTS, fixed={"mode": "ampere",
                                                               "voltage_mV": 3700}, log=quiet)
        cls.srv = LiveServer(LiveFeed(history_s=2), 0, host="127.0.0.1", data_dir=cls.tmp.name,
                             projects=cls.store, token="geheim")
        cls.base = f"http://127.0.0.1:{cls.srv.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.read()

    def post(self, obj, token="geheim", ctype="application/json"):
        req = urllib.request.Request(self.base + "/api/projekte", data=json.dumps(obj).encode(),
                                     headers={"Content-Type": ctype, "X-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_seite_und_liste(self):
        code, body = self.get("/projekte")
        self.assertEqual(code, 200)
        self.assertIn(b"Neues Projekt", body)
        j = json.loads(self.get("/api/projekte")[1])
        self.assertTrue(j["schutz"])
        self.assertEqual(j["fest"], {"mode": "ampere", "voltage_mV": 3700})
        self.assertEqual(j["vorgaben"]["battery_mAh"], 3000.0)
        self.assertEqual(j["aktiv"], j["projekte"][0]["slug"])

    def test_anlegen_wechseln_schutz(self):
        self.assertEqual(self.post({"aktion": "neu", "name": "A"}, token="falsch")[0], 403)
        self.assertEqual(self.post({"aktion": "neu", "name": "A"}, ctype="text/plain")[0], 415)
        code, j = self.post({"aktion": "neu", "name": "A", "settings": {"battery_mAh": -1}})
        self.assertEqual(code, 400)
        self.assertIn("battery_mAh", j["error"])
        code, j = self.post({"aktion": "neu", "name": "Board API", "board": "ESP32-S3",
                             "settings": {"quiet_max_uA": 800}})
        self.assertEqual(code, 200)
        self.assertEqual(j["aktiv"], "board-api")
        self.assertEqual(self.store.snapshot()[1]["settings"]["quiet_max_uA"], 800.0)
        code, j = self.post({"aktion": "aendern", "slug": "board-api", "notiz": "Test"})
        self.assertEqual(self.store.get("board-api")["notiz"], "Test")
        old = [p["slug"] for p in j["projekte"] if not p["aktiv"]][0]
        code, j = self.post({"aktion": "aktivieren", "slug": old})
        self.assertEqual(j["aktiv"], old)
        self.assertEqual(self.post({"aktion": "loeschen", "slug": old})[0], 400)

    def test_auswertung_pro_projekt(self):
        j = json.loads(self.get("/api/analyse?projekt=gibt-es-nicht")[1])
        self.assertIn("error", j)
        j = json.loads(self.get("/wakes?projekt=..%2F..")[1])
        self.assertEqual(j, {"wakes": [], "available": False})
        # Archiviertes Projekt mit Daten: Zusammenfassung im Hintergrund
        s = self.store.create("Mit Daten", activate=False)
        with open(os.path.join(self.store.dir_of(s), "ppk2_2026-11-20.csv"), "w") as f:
            f.write(HEADER)
            for k in range(600):
                f.write(f"2026-11-20T10:{k // 60:02d}:{k % 60:02d}+00:00,150,1,2,100000,0,150\n")
        for _ in range(100):
            m = [p for p in json.loads(self.get("/api/projekte")[1])["projekte"]
                 if p["slug"] == s][0]
            if "berechne" not in m["zusammenfassung"]:
                break
            time.sleep(0.1)
        z = m["zusammenfassung"]
        self.assertAlmostEqual(z["quiet_uA"], 150.0, delta=0.1)
        self.assertAlmostEqual(z["measured_h"], 599 / 3600, delta=0.001)
        j = json.loads(self.get(f"/api/analyse?projekt={s}&hours=1000000")[1])
        self.assertAlmostEqual(j["avg_uA"], 150.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
