# SPDX-License-Identifier: GPL-2.0-only
"""FastDecoder gegen PPK2_API.get_samples() (ppk2-api 0.9.2), ohne Hardware.

1. Synthetische Rohwörter mit Bereichswechseln (Spike-Filter der Bibliothek aktiv).
2. Echte Rohbytes aus tests/daten/*.bin (Mitschnitt mit `diag.py --raw`, Kalibrierdaten
   in der .json daneben). Fehlt die Datei, wird der Test übersprungen.
Außerdem die Zählerauswertung von diag.py.
"""
import glob
import json
import os
import sys
import unittest
from unittest import mock

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ppk2_api.ppk2_api import PPK2_API  # noqa: E402

import diag  # noqa: E402
from ppk2_fast import FastDecoder  # noqa: E402

DATA_DIR = os.path.join(ROOT, "tests", "daten")

# Kalibrierwerte in realistischer Größenordnung, damit alle Terme der Formel wirken
MODIFIERS = {
    "R": {"0": 1031.64, "1": 101.65, "2": 10.15, "3": 0.94, "4": 0.043},
    "GS": {"0": 0.0011, "1": 0.00093, "2": 0.0102, "3": 0.091, "4": 1.2},
    "GI": {"0": 0.9971, "1": 0.9952, "2": 0.9985, "3": 0.9937, "4": 0.9871},
    "O": {"0": 112.3, "1": 77.8, "2": 64.1, "3": 58.2, "4": 61.9},
    "S": {"0": 1.3e-8, "1": 2.6e-8, "2": 3.8e-7, "3": 5.1e-6, "4": 1.1e-4},
    "I": {"0": -2.1e-8, "1": -4.4e-8, "2": -1.7e-7, "3": 3.3e-6, "4": 1.9e-4},
    "UG": {"0": 1.0, "1": 1.0, "2": 1.0, "3": 1.0, "4": 1.0},
}


def make_api(modifiers, vdd):
    """PPK2_API ohne seriellen Port."""
    with mock.patch("serial.Serial"):
        p = PPK2_API("fake")
    for k, v in modifiers.items():
        if isinstance(v, dict):
            p.modifiers[k] = {i: float(x) for i, x in v.items()}
    p.current_vdd = vdd
    return p


def synth_words(n, seed=0):
    """Rohwörter: ADC in Bit 0-13, Bereich in Bit 14-16, Zähler in Bit 18-23.
    Bereich wechselt in Abschnitten (Ruhe in Bereich 1/2, Pulse in Bereich 3/4)."""
    rng = np.random.default_rng(seed)
    rngs = np.empty(n, dtype=np.uint32)
    pos = 0
    while pos < n:
        length = int(rng.choice([1, 2, 5, 40, 3000, 30000]))
        rngs[pos:pos + length] = rng.choice([0, 1, 2, 3, 4], p=[0.05, 0.4, 0.3, 0.15, 0.1])
        pos += length
    adc = rng.integers(200, 16000, n).astype(np.uint32)
    counter = (np.arange(n) % 64).astype(np.uint32)
    logic = rng.integers(0, 256, n).astype(np.uint32)
    return (adc | (rngs << 14) | (counter << 18) | (logic << 24)).astype("<u4")


def compare(test, data, modifiers, vdd, chunk_sizes):
    ref = make_api(modifiers, vdd)
    fast_api = make_api(modifiers, vdd)
    dec = FastDecoder(fast_api)
    want, got = [], []
    pos, i = 0, 0
    while pos < len(data):
        n = chunk_sizes[i % len(chunk_sizes)]
        blk = data[pos:pos + n]
        pos += len(blk)
        i += 1
        s, _ = ref.get_samples(blk)
        want.extend(s)
        got.append(dec.decode(blk))
    got = np.concatenate(got)
    test.assertEqual(len(got), len(want))
    np.testing.assert_allclose(got, np.asarray(want), rtol=1e-9, atol=1e-6)
    # Filterzustand muss für den nächsten Block (und get_samples-Fallback) stimmen
    for attr in ("prev_range", "after_spike", "consecutive_range_samples"):
        test.assertEqual(getattr(fast_api, attr), getattr(ref, attr), attr)
    for attr in ("rolling_avg", "rolling_avg4"):
        test.assertAlmostEqual(getattr(fast_api, attr) / getattr(ref, attr), 1.0, places=9)
    return len(got)


class DecoderTest(unittest.TestCase):
    def test_synthetisch(self):
        data = synth_words(400_000).tobytes()
        # Blockgrößen wie PPK2_MP (40 000) und krumme Größen (Wortgrenze mitten im Block)
        n = compare(self, data, MODIFIERS, 3700, [40_000, 4_001, 39_999, 7, 120_000])
        self.assertEqual(n, 400_000)

    def test_wechsel_direkt_nach_schnellem_block(self):
        # Je 2 Blöcke à 10 000 Samples ein Bereich: Block 1 schnell, Block 2 beginnt mit dem
        # Wechsel und nutzt sofort den nachgeführten Filterzustand (rolling_avg/rolling_avg4)
        w = synth_words(200_000, seed=3)
        seg = (np.arange(len(w)) // 20_000) % 2
        rng = np.where(seg == 0, 2, 4).astype(np.uint32)
        w = (w & ~np.uint32(0x7 << 14)) | (rng << 14)
        compare(self, w.astype("<u4").tobytes(), MODIFIERS, 3700, [40_000])

    def test_fenster_ueber_blockgrenze(self):
        # Viele Wechsel, winzige Blöcke: Spike-Fenster (3 Samples) reicht oft in den
        # nächsten Block, auch mehrere Blöcke weit
        w = synth_words(30_000, seed=5)
        rng = np.random.default_rng(7).choice([1, 2, 4], size=len(w)).astype(np.uint32)
        w = (w & ~np.uint32(0x7 << 14)) | (rng << 14)
        compare(self, w.astype("<u4").tobytes(), MODIFIERS, 3700, [4, 8, 12, 4, 400, 6, 8004])

    def test_echter_mitschnitt(self):
        files = sorted(glob.glob(os.path.join(DATA_DIR, "*.bin")))
        if not files:
            self.skipTest("kein Mitschnitt in tests/daten/")
        for path in files:
            with self.subTest(datei=os.path.basename(path)):
                with open(os.path.splitext(path)[0] + ".json") as f:
                    meta = json.load(f)
                with open(path, "rb") as f:
                    data = f.read()
                off = diag.counter_stats(data)["offset"]
                data = data[off:]
                sizes = [n for _, n in meta.get("reads", [])] or [40_000]
                compare(self, data, meta["modifiers"], meta["current_vdd"], sizes)


class CounterTest(unittest.TestCase):
    def test_lueckenlos(self):
        s = diag.counter_stats(synth_words(50_000).tobytes())
        self.assertEqual((s["offset"], s["gaps"], s["missing_min"]), (0, 0, 0))

    def test_verworfene_bloecke(self):
        # wie PPK2_MP: ganze 40 000-Byte-Blöcke fehlen (10 000 Samples, 10000 % 64 = 16)
        w = synth_words(100_000)
        blocks = [w[i:i + 10_000] for i in range(0, len(w), 10_000)]
        data = b"".join(b.tobytes() for k, b in enumerate(blocks) if k not in (3, 7))
        s = diag.counter_stats(data)
        self.assertEqual(s["gaps"], 2)
        self.assertEqual(s["gaps_at_chunk"], 2)
        self.assertEqual(s["missing_min"], 2 * (10_000 % 64))
        self.assertEqual(s["missing_est"], 20_000)

    def test_einzelne_samples_fehlen_und_versatz(self):
        w = np.delete(synth_words(20_000), [1001, 1002, 5000])
        s = diag.counter_stats(b"\x00\x01" + w.tobytes())
        self.assertEqual(s["offset"], 2)
        self.assertEqual(s["gaps"], 2)
        self.assertEqual(s["missing_min"], 3)
        self.assertEqual(s["gaps_at_chunk"], 0)


if __name__ == "__main__":
    unittest.main()
