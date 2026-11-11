# SPDX-License-Identifier: GPL-2.0-only
"""Schneller numpy-Dekoder für PPK2-Rohdaten.

Rechnet dasselbe wie PPK2_API.get_samples() (ppk2-api 0.9.2) einschließlich des
Spike-Filters der Bibliothek, aber vektorisiert. Der Filter der Bibliothek pro Sample:

    ra  = a  * adc + (1 - a)  * ra        (a  = spike_filter_alpha,  0.18)
    ra4 = a5 * adc + (1 - a5) * ra4       (a5 = spike_filter_alpha5, 0.06)
    Bereichswechsel: after_spike = 3, consecutive_range_samples = 0
    solange after_spike > 0 (Wechsel-Sample und die zwei folgenden):
        Bereich 4 und consecutive < 2: ra/ra4 nicht aktualisieren (zurücksetzen)
        Ausgabe = ra4 (Bereich 4) bzw. ra (sonst), after_spike -= 1
    sonst Ausgabe = adc

Das ist ein exponentielles Mittel über alle Samples außer den zurückgesetzten. Es wird
blockweise per Matrixprodukt berechnet (nur abklingende Potenzen, numerisch stabil).
Der gepulste Ruhestrom des Prüflings wechselt ca. 11 000-mal pro Sekunde den Bereich,
deshalb muss auch der Fall mit Wechseln schnell sein.

Der Filterzustand (rolling_avg, rolling_avg4, prev_range, after_spike,
consecutive_range_samples) wird im Bibliotheksobjekt nachgeführt. Nur der allererste
Block (Zustand noch leer) geht durch get_samples() der Bibliothek.
"""
import numpy as np

_K = 64  # Blockgröße für das exponentielle Mittel


class FastDecoder:
    def __init__(self, ppk2):
        self.p = ppk2
        self.rem = b""
        self._tab = None
        self._tab_vdd = None
        self._ema_mats = {}

    def _tables(self):
        m = self.p.modifiers

        def arr(key):
            return np.array([float(m[key][str(i)]) for i in range(5)])

        self._tab = {k: arr(k) for k in ("O", "R", "UG", "GS", "GI", "S", "I")}
        self._tab_vdd = self.p.current_vdd

    def _convert(self, adc, rng):
        t = self._tab
        vdd = (self.p.current_vdd or 0) / 1000.0
        rwg = (adc - t["O"][rng]) * (self.p.adc_mult / t["R"][rng])
        out = t["UG"][rng] * (rwg * (t["GS"][rng] * rwg + t["GI"][rng])
                              + (t["S"][rng] * vdd + t["I"][rng]))
        return out  # in Ampere

    def _ema(self, x, alpha, y0):
        """y[i] = alpha * x[i] + (1 - alpha) * y[i-1], y[-1] = y0. Gibt alle y zurück."""
        m = len(x)
        if m == 0:
            return x
        mats = self._ema_mats.get(alpha)
        if mats is None:
            i = np.arange(_K)
            diff = i[:, None] - i[None, :]
            w = np.where(diff >= 0, alpha * (1.0 - alpha) ** np.maximum(diff, 0), 0.0)
            d = (1.0 - alpha) ** (i + 1)
            mats = (w.T.copy(), d)
            self._ema_mats[alpha] = mats
        wt, d = mats
        c = -(-m // _K)
        xp = np.zeros(c * _K)
        xp[:m] = x
        local = xp.reshape(c, _K) @ wt          # Mittel je Teilblock mit Startwert 0
        dk = float(d[-1])
        starts = np.empty(c)
        s = y0
        for j, last in enumerate(local[:, -1].tolist()):
            starts[j] = s
            s = dk * s + last
        y = local + starts[:, None] * d[None, :]
        return y.reshape(-1)[:m]

    def decode(self, buf):
        """Rohbytes -> numpy-Array mit Stromwerten in µA."""
        data = self.rem + buf
        n = len(data) - (len(data) % 4)
        self.rem = data[n:]
        if n == 0:
            return np.empty(0)
        p = self.p
        if p.prev_range is None or p.rolling_avg is None or p.rolling_avg4 is None:
            samples, _ = p.get_samples(data[:n])
            return np.asarray(samples, dtype=np.float64)
        if self._tab is None or self._tab_vdd != p.current_vdd:
            self._tables()

        raw = np.frombuffer(data, dtype="<u4", count=n // 4)
        cnt = len(raw)
        rng = np.minimum((raw >> 14) & 0x7, 4).astype(np.intp)
        amp = self._convert(((raw & 0x3FFF) * 4).astype(np.float64), rng)
        idx = np.arange(cnt)

        # Letzter Bereichswechsel an oder vor jedem Sample. Ein noch laufendes Fenster aus
        # dem vorigen Block wird als virtueller Wechsel bei -(3 - after_spike) eingesetzt.
        prev = np.empty(cnt, dtype=np.intp)
        prev[0] = int(p.prev_range)
        prev[1:] = rng[:-1]
        switch = rng != prev
        a0 = int(p.after_spike)
        virtual = -(3 - a0) if a0 > 0 else -(1 << 40)
        last = np.maximum.accumulate(np.where(switch, idx, virtual))
        k = idx - last                       # = consecutive_range_samples im Fenster
        filt = k <= 2
        is4 = rng == 4
        skip = filt & is4 & (k < 2)          # Bibliothek setzt ra/ra4 zurück

        keep = ~skip
        inc = amp[keep]
        ra_inc = self._ema(inc, p.spike_filter_alpha, float(p.rolling_avg))
        ra4_inc = self._ema(inc, p.spike_filter_alpha5, float(p.rolling_avg4))
        pos = np.cumsum(keep) - 1            # letztes einbezogenes Sample
        ra_all = np.concatenate(([float(p.rolling_avg)], ra_inc))
        ra4_all = np.concatenate(([float(p.rolling_avg4)], ra4_inc))

        out = amp.copy()
        sel = filt & ~is4
        out[sel] = ra_all[pos[sel] + 1]
        sel = filt & is4
        out[sel] = ra4_all[pos[sel] + 1]

        # Zustand der Bibliothek nachführen
        p.rolling_avg = float(ra_all[-1])
        p.rolling_avg4 = float(ra4_all[-1])
        p.prev_range = str(int(rng[-1]))
        lastsw = int(last[-1])
        if lastsw > -(1 << 39):
            p.after_spike = max(0, 3 - (cnt - lastsw))
            p.consecutive_range_samples = min(cnt - 1 - lastsw, 2)
        return out * 1e6
