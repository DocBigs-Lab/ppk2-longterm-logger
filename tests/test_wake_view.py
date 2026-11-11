# SPDX-License-Identifier: GPL-2.0-only
"""Smoke-Test für wake_view.py: Liste, Tagesübersicht, Verlauf, CSV-Export."""
import csv
import os
import subprocess
import sys
import tempfile
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [ROOT, HERE]

from sim import feed, segment, wake_phase  # noqa: E402
from wake_capture import WakeRecorder  # noqa: E402


class WakeViewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        r = WakeRecorder(cls.tmp.name, log=lambda s: None, min_free_mb=0, trigger_ms=1)
        feed(r, np.concatenate((segment(3, 150.0), wake_phase(), segment(3, 150.0))))
        r.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_view(self, *args, ohne_numpy=False, check=True):
        env = dict(os.environ)
        if ohne_numpy:      # numpy.py, das beim Import scheitert, vor den Suchpfad legen
            block = os.path.join(self.tmp.name, "ohne_numpy")
            os.makedirs(block, exist_ok=True)
            with open(os.path.join(block, "numpy.py"), "w") as f:
                f.write("raise ImportError('numpy blockiert')\n")
            env["PYTHONPATH"] = block
        p = subprocess.run([sys.executable, os.path.join(ROOT, "wake_view.py"),
                            "--dir", self.tmp.name, *args],
                           capture_output=True, text=True, check=check, env=env)
        return p if not check else p.stdout

    def test_liste(self):
        out = self.run_view()
        self.assertIn("25.50", out)
        self.assertIn(".npz", out)

    def test_tage(self):
        self.assertIn("255.", self.run_view("--tage", "2"))

    def test_verlauf(self):
        out = self.run_view("letzte", "--raster", "5")
        self.assertIn("aktiv 25.50 s", out)
        self.assertIn("Vorlauf (Ruhestrom)", out)

    def test_ohne_numpy(self):
        self.assertIn("25.50", self.run_view(ohne_numpy=True))
        self.assertIn("255.", self.run_view("--tage", "2", ohne_numpy=True))
        p = self.run_view("letzte", ohne_numpy=True, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("numpy fehlt", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_csv(self):
        path = os.path.join(self.tmp.name, "h.csv")
        self.run_view("letzte", "--csv", path, "--bucket-ms", "10")
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertIn(len(rows), (2950, 2951))   # ggf. ein angebrochenes Raster am Ende
        self.assertAlmostEqual(float(rows[0]["t_s"]), -2.0)


if __name__ == "__main__":
    unittest.main()
