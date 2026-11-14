#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""PPK2-Diagnose: messen und ausgeben, was wirklich vom Gerät ankommt.

Aufruf (Logger vorher stoppen, das PPK2 kann nur ein Programm gleichzeitig nutzen):
  python diag.py                                  5 s, wie der Logger (PPK2_MP mit Lese-Thread)
  python diag.py --sekunden 60 --raw data/roh.bin Rohbytes mitschneiden (+ data/roh.json)
  python diag.py --sekunden 60 --direkt           ohne Lese-Thread der Bibliothek lesen
  python diag.py --auswerten data/roh.bin         vorhandenen Mitschnitt auswerten, ohne PPK2

Zählerbits: Jedes 4-Byte-Wort enthält in Bit 18-23 einen 6-Bit-Zähler, der pro Sample um 1
weiterzählt. Sprünge zeigen fehlende Samples. Liegen sie auf den 0,1-s-Blockgrenzen von
PPK2_MP (40 000 Bytes), hat die Warteschlange der Bibliothek Blöcke verworfen.
Grenze: Der Zähler läuft nur bis 64. Ein verworfener Block (10 000 Samples) erscheint als
Sprung um 17, vier Blöcke am Stück (40 000 = 625 x 64) wären unsichtbar. Deshalb auch mit
--direkt messen und die Raten vergleichen.
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

import numpy as np

RATE_NOMINAL = 100_000
MP_CHUNK_BYTES = 40_000   # PPK2_MP: buffer_chunk_seconds=0.1 -> 10 000 Samples à 4 Bytes


def counter_stats(data):
    """Zählerbits 18-23 auswerten. Wählt den Byte-Versatz (0-3), bei dem der Zähler am
    häufigsten um genau 1 steigt, falls der Mitschnitt nicht auf einer Wortgrenze beginnt."""
    best = None
    for off in range(4):
        n = (len(data) - off) // 4
        if n < 2:
            continue
        w = np.frombuffer(data, dtype="<u4", count=n, offset=off)
        c = ((w >> 18) & 0x3F).astype(np.int16)
        d = np.diff(c) % 64
        ok = int(np.count_nonzero(d == 1))
        if best is None or ok > best[0]:
            best = (ok, off, d)
    if best is None:
        return None
    ok, off, d = best
    gap_idx = np.flatnonzero(d != 1)          # Sprung zwischen Wort i und i+1
    missing = int(((d[gap_idx] - 1) % 64).sum())
    # Byte-Position des Worts nach dem Sprung, relativ zum Anfang des Mitschnitts
    gap_bytes = off + (gap_idx + 1) * 4
    at_chunk = gap_bytes % MP_CHUNK_BYTES == 0
    return {
        "offset": off,
        "words": int(len(d) + 1),
        "steps_ok": ok,
        "gaps": int(len(gap_idx)),
        "missing_min": missing,   # untere Grenze: Sprünge über 64 Samples zählen nur modulo 64
        "gaps_at_chunk": int(np.count_nonzero(at_chunk)),
        # Schätzung: Sprung an einer Blockgrenze = ein ganzer Block (10 000 Samples) verworfen
        "missing_est": int(((d[gap_idx[~at_chunk]] - 1) % 64).sum())
        + int(np.count_nonzero(at_chunk)) * (MP_CHUNK_BYTES // 4),
        "hist": Counter(int(x) for x in d[gap_idx]).most_common(8),
        "first_gaps": [int(x) for x in gap_bytes[:10]],
    }


def print_report(data, wall_s=None, chunk_sizes=None):
    n = len(data) // 4
    print(f"\n{len(data)} Bytes = {n} Samples", end="")
    if wall_s:
        print(f" in {wall_s:.2f} s -> {n / wall_s:.0f} Samples/s "
              f"({100 * n / wall_s / RATE_NOMINAL:.1f} % von 100 kSps)")
    else:
        print()
    if chunk_sizes:
        sz = Counter(chunk_sizes).most_common(3)
        print(f"Lesevorgänge mit Daten: {len(chunk_sizes)}, häufigste Größen (Bytes, Anzahl): {sz}")

    s = counter_stats(data)
    if s is None:
        print("Zu wenig Daten für die Zählerauswertung")
        return
    share = s["steps_ok"] / max(s["words"] - 1, 1)
    print(f"Zähler (Bit 18-23): Versatz {s['offset']} Bytes, {100 * share:.2f} % der Schritte = +1")
    if share < 0.5:
        print("  Der Zähler steigt nicht pro Sample um 1. Auswertung nicht aussagekräftig.")
        print(f"  Häufigste Schritte: {s['hist']}")
        return
    print(f"  Sprünge: {s['gaps']}, davon auf {MP_CHUNK_BYTES}-Byte-Blockgrenzen: {s['gaps_at_chunk']}")
    est = s["missing_est"]
    print(f"  fehlende Samples geschätzt: {est} ({100 * est / max(n + est, 1):.2f} %), "
          f"Sprünge an Blockgrenzen als je 1 Block à {MP_CHUNK_BYTES // 4} gezählt")
    if s["gaps"]:
        print(f"  Schrittweiten der Sprünge (Schritt, Anzahl): {s['hist']}")
        print(f"  erste Sprünge bei Byte: {s['first_gaps']}")
    if wall_s:
        print(f"  Rate inkl. geschätzter Lücken: {(n + est) / wall_s:.0f} Samples/s")
    if s["gaps"] == 0:
        print("  -> Keine Lücken: Was ankommt, ist lückenlos. Fehlt Rate, liefert das PPK2 weniger.")
    elif s["gaps_at_chunk"] >= 0.9 * s["gaps"]:
        print("  -> Lücken fast nur an Blockgrenzen: PPK2_MP verwirft Blöcke (Leser zu langsam).")
    else:
        print("  -> Lücken mitten in Blöcken: Verlust beim seriellen Lesen (USB/Puffer).")


def quiesce(p):
    """Laufenden Datenstrom stoppen und restliche Bytes verwerfen (wie im Logger)."""
    from ppk2_api.ppk2_api import PPK2_API
    for _ in range(6):
        try:
            PPK2_API.stop_measuring(p)
        except Exception:
            pass
        time.sleep(0.3)
        t_end = time.time() + 3
        while p.ser.in_waiting and time.time() < t_end:
            p.ser.read(p.ser.in_waiting)
            time.sleep(0.05)
        time.sleep(0.3)
        if not p.ser.in_waiting:
            p.ser.reset_input_buffer()
            return True
    return False


def measure(a):
    from ppk2_api.ppk2_api import PPK2_API, PPK2_MP
    from ppk2_fast import FastDecoder

    devs = PPK2_API.list_devices()
    print("Gefundene Ports:", devs)
    if not a.port and not devs:
        sys.exit("Kein PPK2 gefunden")
    port = a.port or sorted(devs)[0]
    print(f"Teste {port} im Modus {a.mode} für {a.sekunden:.0f} s, "
          f"{'direkt (ohne Lese-Thread)' if a.direkt else 'PPK2_MP wie der Logger'}")

    p = PPK2_API(port) if a.direkt else PPK2_MP(port)
    try:
        ok = p.get_modifiers()
    except Exception:
        ok = None
    if not ok:
        print("Alter Datenstrom gestoppt:", quiesce(p))
        ok = p.get_modifiers()
    print("get_modifiers:", ok)

    if a.mode == "ampere":
        p.use_ampere_meter()
        p.current_vdd = a.vdd      # nur die Variable, kein Befehl ans Gerät (wie der Logger)
    else:
        p.use_source_meter()
        p.set_source_voltage(a.vdd)
    p.toggle_DUT_power("ON")
    time.sleep(0.5)
    if a.direkt:
        p.ser.reset_input_buffer()
    p.start_measuring()
    print("start_measuring gesendet")

    dec = FastDecoder(p)
    chunks, times = [], []
    decoded, decode_time = 0, 0.0
    t0 = time.time()
    while time.time() - t0 < a.sekunden:
        raw = PPK2_API.get_data(p) if a.direkt else p.get_data()
        if raw:
            now = time.time()
            if a.raw:
                chunks.append(raw)
            times.append((round(now - t0, 4), len(raw)))
            if not a.direkt or a.dekodieren:
                d0 = time.time()
                samples = dec.decode(raw)
                decode_time += time.time() - d0
                decoded += len(samples)
                if decoded == len(samples):
                    print("erste Bytes (hex):", raw[:32].hex())
                    print("erste Samples (uA):", [round(float(x), 1) for x in samples[:5]])
        time.sleep(0.0005 if a.direkt else 0.005)
    wall = time.time() - t0
    try:
        p.stop_measuring()
    except Exception as e:
        print("stop_measuring:", repr(e))

    total = sum(n for _, n in times)
    if decoded:
        print(f"{decoded} Samples dekodiert, Dekodierzeit gesamt {decode_time:.1f} s")
    stats = getattr(getattr(p, "_fetcher", None), "_stats", None)
    if stats:
        print("letzte Statistik des Lese-Threads (Bytes, s):", stats)

    if a.raw:
        data = b"".join(chunks)
        with open(a.raw, "wb") as f:
            f.write(data)
        meta = {
            "format": 1,
            "port": port, "mode": a.mode, "direkt": a.direkt,
            "current_vdd": p.current_vdd,
            "modifiers": p.modifiers,
            "start_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
            "wall_s": round(wall, 3),
            "bytes": len(data),
            "reads": times,          # (Sekunden seit Start, Bytes) pro Lesevorgang
        }
        with open(os.path.splitext(a.raw)[0] + ".json", "w") as f:
            json.dump(meta, f)
        print(f"Mitschnitt: {a.raw} ({len(data)} Bytes) und {os.path.splitext(a.raw)[0]}.json")
        print_report(data, wall, [n for _, n in times])
    else:
        print(f"\n{total} Bytes in {wall:.1f} s -> {total / 4 / wall:.0f} Samples/s "
              f"(Zählerauswertung braucht --raw)")


def evaluate(path):
    with open(path, "rb") as f:
        data = f.read()
    wall, sizes = None, None
    meta_path = os.path.splitext(path)[0] + ".json"
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
        wall = meta.get("wall_s")
        sizes = [n for _, n in meta.get("reads", [])]
        print(f"{path}: {meta.get('start_utc')}, Modus {meta.get('mode')}, "
              f"{'direkt' if meta.get('direkt') else 'PPK2_MP'}")
    print_report(data, wall, sizes)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("port", nargs="?", default="", help="Port, leer = niedrigster PPK2-Port")
    ap.add_argument("mode", nargs="?", default="ampere", choices=("ampere", "source"))
    ap.add_argument("sek", nargs="?", type=float, help=argparse.SUPPRESS)  # alte Schreibweise
    ap.add_argument("--sekunden", type=float, default=5.0)
    ap.add_argument("--vdd", type=int, default=int(os.getenv("PPK2_VOLTAGE_MV", "3300")),
                    help="mV, wie PPK2_VOLTAGE_MV")
    ap.add_argument("--raw", help="Rohbytes in diese Datei schreiben (Metadaten daneben als .json)")
    ap.add_argument("--direkt", action="store_true",
                    help="ohne Lese-Thread lesen (zeigt, was das PPK2 liefert)")
    ap.add_argument("--dekodieren", action="store_true",
                    help="mit --direkt trotzdem dekodieren (kostet Lesezeit)")
    ap.add_argument("--auswerten", metavar="DATEI", help="Mitschnitt auswerten, ohne PPK2")
    a = ap.parse_args()
    if a.sek is not None:
        a.sekunden = a.sek
    if a.auswerten:
        evaluate(a.auswerten)
    else:
        measure(a)


if __name__ == "__main__":
    main()
