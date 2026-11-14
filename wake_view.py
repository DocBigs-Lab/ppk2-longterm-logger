#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Auswertung der aufgenommenen Wachphasen (data/wake, siehe wake_capture.py).

Liste und --tage laufen ohne Zusatzpakete. Verlauf und --csv brauchen numpy,
--png zusätzlich matplotlib. Im Container ist numpy vorhanden, matplotlib nicht:
    docker exec ppk2-logger python wake_view.py                 # Liste der letzten Wachphasen
    docker exec ppk2-logger python wake_view.py --tage 3        # Zusammenfassung pro Tag
    docker exec ppk2-logger python wake_view.py letzte          # Verlauf der letzten Wachphase
    docker exec ppk2-logger python wake_view.py 2026-11-09/wake_20261109T050012Z.npz --raster 0.5
    python3 wake_view.py letzte --csv phase.csv --bucket-ms 1   # Hülle (min/mittel/max) exportieren
    python3 wake_view.py letzte --png phase.png                 # Diagramm (matplotlib)

--serve ist für die Webseite (live_view.py): Anfragen als JSON-Zeilen auf stdin,
Antworten als JSON-Zeilen auf stdout. Läuft als eigener Prozess, damit das Laden und
Verdichten großer Dateien den Lese-Thread des Loggers nicht ausbremst.
"""
import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

try:
    import numpy as np
except ImportError:          # Liste und Tagesübersicht gehen auch ohne
    np = None


def read_index(wdir):
    path = os.path.join(wdir, "wake_index.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def local(iso, tz):
    t = datetime.fromisoformat(iso) + timedelta(hours=tz)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def show_list(rows, n, tz):
    print(f"{'Start (Ortszeit)':19}  {'aktiv s':>8}  {'µAh':>7}  {'Spitze 1 ms':>11}  Datei")
    for r in rows[-n:]:
        flag = " (abgeschnitten)" if r["truncated"] == "1" else ""
        f = r["file"] if r["status"] == "ok" else r["status"]
        print(f"{local(r['trigger_utc'], tz):19}  {float(r['active_s']):8.2f}  "
              f"{float(r['active_uAh']):7.1f}  {float(r['peak_1ms_uA']) / 1000:8.1f} mA  "
              f"{f}{flag}")


def show_days(rows, days, tz):
    per = defaultdict(list)
    for r in rows:
        per[local(r["trigger_utc"], tz)[:10]].append(r)
    print(f"{'Tag':10}  {'Anzahl':>6}  {'µAh gesamt':>10}  {'µAh Mittel':>10}  {'s Mittel':>8}")
    for day in sorted(per)[-days:]:
        rs = per[day]
        uah = [float(r["active_uAh"]) for r in rs]
        sec = [float(r["active_s"]) for r in rs]
        print(f"{day:10}  {len(rs):6}  {sum(uah):10.1f}  {sum(uah) / len(uah):10.1f}  "
              f"{sum(sec) / len(sec):8.1f}")


def load(path):
    with np.load(path, allow_pickle=False) as z:
        return z["uA"].astype(np.float64), json.loads(str(z["meta"]))


def time_rate(meta):
    """Samples pro Sekunde für Zeitachse und Ladung: effektive Rate, falls gespeichert
    (ab Format 2), sonst nominal."""
    return meta.get("rate_eff") or meta["rate"]


def envelope(a, n):
    """Min/Mittel/Max je n Samples (letzter Rest wird mitgenommen)."""
    m = len(a) // n * n
    blocks = [a[:m].reshape(-1, n)] if m else []
    lo = [b.min(axis=1) for b in blocks]
    me = [b.mean(axis=1) for b in blocks]
    hi = [b.max(axis=1) for b in blocks]
    if m < len(a):
        r = a[m:]
        lo.append([r.min()])
        me.append([r.mean()])
        hi.append([r.max()])
    return (np.concatenate(lo), np.concatenate(me), np.concatenate(hi)) if lo else ([], [], [])


def show_phase(a, meta, raster, tz):
    rate = time_rate(meta)
    pre = meta["pre_samples"]
    print(f"Wachphase {local(meta['trigger_utc'], tz)} (Ortszeit), Trigger bei "
          f"{pre / rate:.3f} s, Schwelle {meta['trigger_uA']:g} µA")
    print(f"  Datei: {len(a)} Samples = {len(a) / rate:.2f} s, aktiv {meta['active_s']:.2f} s, "
          f"{meta['active_uAh']:.1f} µAh (gesamt mit Vor-/Nachlauf {meta['charge_uAh']:.1f} µAh)")
    print(f"  Spitze {meta['peak_uA'] / 1000:.1f} mA (Einzelsample), "
          f"{meta['peak_1ms_uA'] / 1000:.1f} mA (1-ms-Mittel)")
    if meta.get("truncated"):
        print("  Achtung: nach Höchstdauer abgeschnitten")
    if meta.get("aborted"):
        print("  Achtung: Aufnahme beim Beenden des Loggers abgebrochen")
    print(f"  Samples/s: effektiv {rate:.0f} (nominal {meta['rate']})"
          + ("" if "rate_eff" in meta else ", Datei vor Format 2: Zeiten und µAh nominal"))
    est = meta.get("rate_est")
    if est and est < 0.97 * rate:
        print(f"  Achtung: in dieser Aufnahme nur ca. {est:.0f} Samples/s, evtl. Lücken")
    ruhe = a[:pre]
    if len(ruhe):
        print(f"  Vorlauf (Ruhestrom): Mittel {ruhe.mean():.1f} µA")
    act = a[pre:pre + meta["active_samples"]]
    if len(act):
        dt = 1.0 / rate
        print("  Zeit nach Stromstärke (aktiver Teil):")
        edges = [(40_000, None), (20_000, 40_000), (5_000, 20_000), (1_000, 5_000), (0, 1_000)]
        for lo_, hi_ in edges:
            sel = (act >= lo_) if hi_ is None else ((act >= lo_) & (act < hi_))
            s = sel.sum() * dt
            q = act[sel].sum() * dt / 3600.0
            name = f">= {lo_ / 1000:g} mA" if hi_ is None else f"{lo_ / 1000:g}–{hi_ / 1000:g} mA"
            print(f"    {name:>12}: {s:7.2f} s  {q:7.1f} µAh")

    n = max(1, int(round(raster * rate)))
    lo, me, hi = envelope(a, n)
    k = max(1, int(round(rate / 1000)))   # 1-ms-Mittel für das Maximum
    cs = np.concatenate(([0.0], np.cumsum(a)))
    m1 = (cs[k:] - cs[:-k]) / k if len(a) >= k else np.array([a.mean()])
    print(f"\n  {'t (s)':>7}  {'Mittel mA':>9}  {'Max 1 ms':>9}  {'Min mA':>8}  {'µAh kum.':>8}")
    q = 0.0
    for i in range(len(me)):
        a0, a1 = i * n, min((i + 1) * n, len(a))
        q += a[a0:a1].sum() / rate / 3600.0
        # nur 1-ms-Fenster, die ganz in diesem Raster liegen (m1[j] = Mittel a[j:j+k])
        mx = m1[a0:max(a0 + 1, min(a1 - k + 1, len(m1)))].max() if len(m1) > a0 else hi[i]
        t = (a0 - pre) / rate
        print(f"  {t:7.2f}  {me[i] / 1000:9.2f}  {mx / 1000:9.2f}  {lo[i] / 1000:8.3f}  {q:8.1f}")


def export_csv(a, meta, path, bucket_ms):
    rate = time_rate(meta)
    n = max(1, int(round(bucket_ms * rate / 1000)))
    lo, me, hi = envelope(a, n)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "min_uA", "mean_uA", "max_uA"])
        for i in range(len(me)):
            t = (i * n - meta["pre_samples"]) / rate
            w.writerow([f"{t:.5f}", f"{lo[i]:.2f}", f"{me[i]:.2f}", f"{hi[i]:.2f}"])
    print(f"{len(me)} Zeilen nach {path} geschrieben")


def export_png(a, meta, path, tz):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("matplotlib fehlt (pip install matplotlib), --csv geht ohne")
    rate = time_rate(meta)
    n = max(1, len(a) // 4000)               # ca. 4000 Punkte, Hülle aus min/max
    lo, me, hi = envelope(a, n)
    t = (np.arange(len(me)) * n - meta["pre_samples"]) / rate
    fig, ax = plt.subplots(figsize=(12, 4.5))
    ax.fill_between(t, np.asarray(lo) / 1000, np.asarray(hi) / 1000, step="post",
                    alpha=0.35, linewidth=0, label="min/max")
    ax.plot(t, np.asarray(me) / 1000, drawstyle="steps-post", linewidth=0.8, label="Mittel")
    ax.axhline(meta["trigger_uA"] / 1000, linestyle="--", linewidth=0.7, color="gray",
               label="Schwelle")
    ax.set_xlabel("Zeit ab Trigger (s)")
    ax.set_ylabel("Strom (mA)")
    ax.set_title(f"Wachphase {local(meta['trigger_utc'], tz)}: {meta['active_s']:.1f} s, "
                 f"{meta['active_uAh']:.0f} µAh")
    ax.grid(alpha=0.3)
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    print(f"Diagramm nach {path} geschrieben")


def serve(wdir, max_points=20000):
    """Anfrage {"file": rel, "a": Start-Sample, "b": Ende, "n": Punkte} -> Hülle oder Rohwerte.
    Ohne a/b die ganze Datei. Die letzten 3 Dateien bleiben im Speicher (float32)."""
    cache = {}
    root = os.path.realpath(wdir)
    for line in sys.stdin:
        try:
            q = json.loads(line)
            path = os.path.realpath(os.path.join(root, q["file"]))
            if not path.startswith(root + os.sep) or not path.endswith(".npz"):
                raise ValueError("ungültiger Pfad")
            if path not in cache:
                if len(cache) >= 3:
                    cache.pop(next(iter(cache)))
                with np.load(path, allow_pickle=False) as z:
                    cache[path] = (z["uA"].astype(np.float32), json.loads(str(z["meta"])))
            arr, meta = cache[path]
            total = len(arr)
            a0 = max(0, min(total, int(q.get("a", 0))))
            b0 = max(a0, min(total, int(q.get("b", total))))
            n = max(10, min(max_points, int(q.get("n", 2000))))
            seg = arr[a0:b0].astype(np.float64)
            per = max(1, -(-len(seg) // n))
            if per == 1:
                lo = me = hi = seg
            else:
                lo, me, hi = envelope(seg, per)
            r = time_rate(meta)
            out = {"file": q["file"], "total": total, "a": a0, "b": b0, "per": per,
                   "rate": r, "start_epoch": meta.get("start_epoch"),
                   "trigger_epoch": meta.get("trigger_epoch"),
                   "pre_samples": meta.get("pre_samples"),
                   "active_samples": meta.get("active_samples"),
                   "seg_uAh": float(seg.sum()) / r / 3600.0 if len(seg) else 0.0,
                   "seg_mean_uA": float(seg.mean()) if len(seg) else None,
                   "seg_max_uA": float(seg.max()) if len(seg) else None,
                   "min": [round(float(v), 3) for v in lo],
                   "mean": [round(float(v), 3) for v in me],
                   "max": [round(float(v), 3) for v in hi]}
            if q.get("meta"):
                out["meta"] = meta
        except Exception as e:              # Fehler melden, Prozess läuft weiter
            out = {"error": f"{type(e).__name__}: {e}"}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("datei", nargs="?",
                    help="Pfad zur .npz, relativ zu data/wake oder 'letzte'. Ohne: Liste")
    ap.add_argument("--dir", default=os.getenv("OUT_DIR", "data"),
                    help="Datenordner des Loggers (darin liegt wake/)")
    ap.add_argument("-n", type=int, default=20, help="Anzahl Zeilen der Liste")
    ap.add_argument("--tage", type=int, default=0, help="Zusammenfassung der letzten N Tage")
    ap.add_argument("--tz", type=float, default=1, help="Ortszeit-Offset zu UTC in h")
    ap.add_argument("--raster", type=float, default=1.0, help="Zeitraster der Tabelle in s")
    ap.add_argument("--csv", help="Hülle als CSV exportieren")
    ap.add_argument("--bucket-ms", type=float, default=1.0, help="Rasterbreite für --csv")
    ap.add_argument("--png", help="Diagramm als PNG speichern (braucht matplotlib)")
    ap.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()

    wdir = os.path.join(a.dir, "wake")
    if a.serve:
        if np is None:
            sys.exit("numpy fehlt")
        serve(wdir)
        return
    rows = read_index(wdir)
    if not a.datei:
        if not rows:
            sys.exit(f"Keine Wachphasen in {wdir}")
        if a.tage:
            show_days(rows, a.tage, a.tz)
        else:
            show_list(rows, a.n, a.tz)
        return

    if a.datei == "letzte":
        ok = [r for r in rows if r["status"] == "ok"]
        if not ok:
            sys.exit(f"Keine gespeicherte Wachphase in {wdir}")
        path = os.path.join(wdir, ok[-1]["file"])
    else:
        path = a.datei if os.path.exists(a.datei) else os.path.join(wdir, a.datei)
    if not os.path.exists(path):
        sys.exit(f"Datei nicht gefunden: {path} (evtl. wegen der Speichergrenze gelöscht)")
    if np is None:
        sys.exit("numpy fehlt (pip install numpy). Liste und --tage gehen ohne, "
                 "im Container ist numpy vorhanden: docker exec ppk2-logger python wake_view.py ...")
    arr, meta = load(path)
    if a.csv:
        export_csv(arr, meta, a.csv, a.bucket_ms)
    if a.png:
        export_png(arr, meta, a.png, a.tz)
    if not a.csv and not a.png:
        show_phase(arr, meta, a.raster, a.tz)


if __name__ == "__main__":
    main()
