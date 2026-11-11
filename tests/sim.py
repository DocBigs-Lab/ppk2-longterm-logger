# SPDX-License-Identifier: GPL-2.0-only
"""Synthetische Stromverläufe für Tests ohne PPK2 (Werte in µA, 100 kSps)."""
import numpy as np

RATE = 100_000


def segment(seconds, ua, noise_rel=0.05, rng=None):
    """Konstanter Strom mit Rauschen (relativ zum Wert)."""
    rng = rng or np.random.default_rng(0)
    n = int(round(seconds * RATE))
    return (ua * (1.0 + noise_rel * rng.standard_normal(n))).astype(np.float32)


def wake_phase(rng=None):
    """Wachphase wie beim ESP32-C6-Display: WLAN 5 s bei ca. 60 mA, kurzer Einbruch,
    Display-Refresh 20 s bei ca. 31 mA. Erwartete Ladung ca. 255,8 µAh."""
    rng = rng or np.random.default_rng(1)
    return np.concatenate((
        segment(5.0, 60_000, 0.3, rng),
        segment(0.5, 2_000, 0.1, rng),     # Einbruch unter die Schwelle
        segment(20.0, 31_000, 0.1, rng),
    ))


WAKE_UAH = (5.0 * 60_000 + 0.5 * 2_000 + 20.0 * 31_000) / 3600.0


def pulsed_sleep(seconds, base_ua=100.0, pulse_ua=30_000.0, pulse_ms=1.0, period_s=0.7,
                 rng=None):
    """Ruhestrom aus kurzen Pulsen wie am echten Prüfling gemessen (Wandler im
    Pulsbetrieb): im 1-ms-Mittel ca. 30 mA, im 100-ms-Mittel unter 1 mA."""
    a = segment(seconds, base_ua, 0.05, rng)
    w = int(pulse_ms * RATE / 1000)
    for start in range(int(0.3 * RATE), len(a) - w, int(period_s * RATE)):
        a[start:start + w] = pulse_ua
    return a


def feed(recorder, signal, chunk=10_000, t0=1_800_000_000.0, jitter=False, seed=0, wait=True,
         wall_rate=RATE):
    """Signal blockweise einspeisen. now = Zeit des letzten Samples im Block.
    wait=True wartet nach jedem Block auf den Schreib-Thread (Simulation läuft schneller
    als Echtzeit, sonst wäre der Schreiber künstlich belegt).
    wall_rate: Samples pro Wanduhr-Sekunde (am echten PPK2 ca. 88 000 statt 100 000)."""
    rng = np.random.default_rng(seed)
    pos = 0
    while pos < len(signal):
        n = int(rng.integers(1, 2 * chunk)) if jitter else chunk
        blk = signal[pos:pos + n]
        pos += len(blk)
        recorder.feed(blk, t0 + (pos - 1) / wall_rate)
        if wait:
            recorder.wait_idle()
    return t0 + (pos - 1) / wall_rate
