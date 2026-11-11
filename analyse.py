#!/usr/bin/env python3
"""Auswertung der PPK2-CSV: Ruhestrom, Wachphasen, Tagesbilanz, Laufzeit.

SPDX-License-Identifier: GPL-2.0-only

Aufruf (im Ordner /opt/ppk2-logger, ohne Docker, nur Python 3):
    python3 analyse.py                       # letzte 24 h
    python3 analyse.py --hours 12
    python3 analyse.py --raster 1800         # Abweichung zu :00/:30 anzeigen
    python3 analyse.py --von 2026-11-07T10:30 # ab Zeitpunkt (UTC)
    python3 analyse.py --hours 72 --png verlauf.png   # Diagramm (braucht matplotlib)
    python3 analyse.py --json                # Ergebnis als JSON (für die Webseite)
"""
import argparse, csv, glob, json, math, os, sys
from datetime import datetime, timedelta, timezone

ap = argparse.ArgumentParser()
ap.add_argument("--dir", default=os.getenv("OUT_DIR", "data"))
ap.add_argument("--hours", type=float, default=24)
ap.add_argument("--von", help="Start, UTC, z.B. 2026-11-07T10:30")
ap.add_argument("--aktiv", type=float, default=5000, help="Schwelle aktiv (uA)")
ap.add_argument("--mah", type=float, default=3000)
ap.add_argument("--nutzbar", type=float, default=0.8)
ap.add_argument("--tz", type=float, default=1, help="Ortszeit-Offset zu UTC in h")
ap.add_argument("--raster", type=int, default=0,
                help="Termin-Raster in Sekunden (z. B. 1800); zeigt die Abweichung der Wachphasen davon. 0 = aus")
ap.add_argument("--redraw-uah", type=float, default=120,
                help="Wachphasen ab dieser Ladung (uAh) gelten als 'teuer' (z. B. Display-Refresh)")
ap.add_argument("--png", help="Diagramm (Ruhestrom pro Stunde, Ladung pro Wachphase) als PNG, braucht matplotlib")
ap.add_argument("--json", action="store_true",
                help="Ergebnis als JSON auf stdout statt Text (Zeiten als Unix-Sekunden, UTC)")
a = ap.parse_args()

# Im JSON-Modus keine Textausgabe, Fehler als {"error": ...}
say = (lambda *x, **k: None) if a.json else print


def fail(msg):
    if a.json:
        print(json.dumps({"error": msg}))
        sys.exit(0)
    raise SystemExit(msg)


def read_csv(p):
    out = []
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            try:
                t = datetime.fromisoformat(r["timestamp_utc"])
                try:
                    mx = float(r["max_uA"])
                except (KeyError, TypeError, ValueError):
                    mx = float(r["mean_uA"])
                out.append((t, float(r["mean_uA"]), mx))
            except (KeyError, ValueError):
                pass
    return out


def file_day(p):
    try:
        return datetime.strptime(os.path.basename(p)[5:15], "%Y-%m-%d").date()
    except ValueError:
        return None


# Nur die Tagesdateien lesen, die den Zeitraum betreffen (neueste zuerst). Ein Tag
# Reserve, weil der Dateiname aus der Uhrzeit des Containers stammt, die Zeilen aus UTC.
von = datetime.fromisoformat(a.von).replace(tzinfo=timezone.utc) if a.von else None
rows, newest = [], None
for p in sorted(glob.glob(os.path.join(a.dir, "ppk2_*.csv")), reverse=True):
    d = file_day(p)
    if newest is not None and d is not None:
        first = von if von else newest - timedelta(hours=a.hours)
        if d < first.date() - timedelta(days=1):
            break
    part = read_csv(p)
    rows.extend(part)
    if part and d is not None:
        m = max(r[0] for r in part)
        newest = m if newest is None or m > newest else newest
rows.sort()
if len(rows) < 2:
    fail("Zu wenig Daten.")

end = rows[-1][0]
if a.von:
    start = datetime.fromisoformat(a.von).replace(tzinfo=timezone.utc)
else:
    start = end - timedelta(hours=a.hours)
rows = [r for r in rows if r[0] >= start]
if len(rows) < 2:
    fail("Zu wenig Daten im Zeitraum.")

# Intervalldauer = Abstand zum Vorgaenger (max. 5 s, Luecken zaehlen nicht voll)
iv = []
for i in range(1, len(rows)):
    dt = (rows[i][0] - rows[i - 1][0]).total_seconds()
    iv.append((rows[i][0], rows[i][1], min(max(dt, 0.0), 5.0)))

tot_s = sum(d for _, _, d in iv)
tot_uah = sum(m * d for _, m, d in iv) / 3600
quiet = [(t, m, d) for t, m, d in iv if m < a.aktiv]
q_s = sum(d for _, _, d in quiet)
q_uah = sum(m * d for _, m, d in quiet) / 3600
q_mean = q_uah * 3600 / q_s if q_s else 0

# Wachphasen: aufeinanderfolgende aktive Intervalle (Luecke <= 3 s)
events, cur = [], []
for t, m, d in iv:
    if m >= a.aktiv:
        if cur and (t - cur[-1][0]).total_seconds() > 3:
            events.append(cur); cur = []
        cur.append((t, m, d))
if cur:
    events.append(cur)

def loc(t):
    return t + timedelta(hours=a.tz)

say(f"Zeitraum: {loc(rows[0][0]):%d.%m. %H:%M} bis {loc(rows[-1][0]):%d.%m. %H:%M} Ortszeit "
      f"({tot_s/3600:.1f} h gemessen)")
say()
say("RUHESTROM (Intervalle unter %.0f mA)" % (a.aktiv / 1000))
say(f"  Mittel:  {q_mean:.0f} uA   ({q_s/tot_s*100:.1f} % der Zeit)")
ms = sorted(m for _, m, _ in quiet)
if ms:
    say(f"  Median der Sekundenwerte: {ms[len(ms)//2]:.0f} uA, "
          f"P5 {ms[int(len(ms)*0.05)]:.0f}, P95 {ms[int(len(ms)*0.95)]:.0f}")

# Stundenwerte Ruhestrom
say("\n  Ruhestrom pro Stunde (Ortszeit):")
per = {}
for t, m, d in quiet:
    k = loc(t).replace(minute=0, second=0, microsecond=0)
    s = per.setdefault(k, [0.0, 0.0]); s[0] += m * d; s[1] += d
hourly = [(k, x / y) for k, (x, y) in per.items() if y > 0]
line = []
for k, v in hourly:
    line.append(f"{k:%d.%m. %H}h {v:.0f}")
for i in range(0, len(line), 6):
    say("   ", " | ".join(line[i:i+6]))

say("\nWACHPHASEN")
ev = []
for e in events:
    dur = sum(d for _, _, d in e)
    uah = sum(m * d for _, m, d in e) / 3600
    ev.append((e[0][0], dur, uah, max(m for _, m, _ in e)))
n = len(ev)
say(f"  Anzahl: {n}")
if n:
    big = [x for x in ev if x[2] >= a.redraw_uah]     # teure Wachphasen
    small = [x for x in ev if x[2] < a.redraw_uah]
    def avg(l, i): return sum(x[i] for x in l) / len(l) if l else 0
    say(f"  teuer (>={a.redraw_uah:.0f} uAh):   {len(big)}, im Mittel {avg(big,2):.0f} uAh, {avg(big,1):.0f} s")
    say(f"  guenstig:               {len(small)}, im Mittel {avg(small,2):.0f} uAh, {avg(small,1):.0f} s")
    head = "\n  Zeit (Ortszeit)  Dauer   Ladung"
    if a.raster:
        head += "   Abw. zum Raster"
    say(head)
    for t, dur, uah, pk in ev:
        lt = loc(t)
        txt = f"  {lt:%d.%m. %H:%M:%S}  {dur:4.0f} s  {uah:5.0f} uAh"
        if a.raster:
            sec = lt.minute * 60 + lt.second
            off = sec - round(sec / a.raster) * a.raster
            txt += f"  {off:+6d} s"
        if uah < a.redraw_uah:
            txt += "  <- guenstig"
        say(txt)

w_uah = sum(x[2] for x in ev)
say("\nBILANZ")
say(f"  Gesamt: {tot_uah/1000:.2f} mAh in {tot_s/3600:.1f} h  -> Schnitt {tot_uah*3600/tot_s:.0f} uA")
say(f"  davon Wachphasen: {w_uah/1000:.2f} mAh ({w_uah/tot_uah*100:.0f} %), Rest Ruhe/Sonstiges {(tot_uah-w_uah)/1000:.2f} mAh")
per_day = tot_uah / tot_s * 86400 / 1000
say(f"  Hochgerechnet: {per_day:.1f} mAh pro Tag")
if per_day > 0:
    say(f"  Akkulaufzeit ({a.mah:.0f} mAh x {a.nutzbar*100:.0f} %): {a.mah*a.nutzbar/per_day:.0f} Tage")


def plot(path):
    """Zwei Diagramme mit gemeinsamer Zeitachse (Ortszeit): Ruhestrom pro Stunde, Ladung pro Wachphase."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        raise SystemExit("matplotlib fehlt (pip install matplotlib), die Textausgabe oben geht ohne")
    ink, muted, grid = "#0b0b0b", "#52514e", "#e4e3df"
    blue, orange = "#2a78d6", "#eb6834"          # Kategorie 1 und 2 (validierte Palette)
    plt.rcParams.update({"font.size": 9, "axes.edgecolor": muted, "axes.labelcolor": ink,
                         "xtick.color": muted, "ytick.color": muted})
    fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(10, 6), facecolor="#fcfcfb")
    for ax in (ax1, ax2):
        ax.set_facecolor("#fcfcfb")
        ax.grid(axis="y", color=grid, linewidth=0.8)
        ax.spines[["top", "right"]].set_visible(False)

    if hourly:
        # Stundenwert in der Mitte der Stunde zeichnen
        ax1.plot([k + timedelta(minutes=30) for k, _ in hourly], [v for _, v in hourly],
                 color=blue, linewidth=2, marker="o", markersize=3)
    ax1.set_ylim(0, max([v for _, v in hourly] or [1]) * 1.2)
    ax1.set_ylabel("µA")
    ax1.set_title(f"Ruhestrom pro Stunde (Intervalle unter {a.aktiv / 1000:.0f} mA), "
                  f"Mittel {q_mean:.0f} µA", loc="left", color=ink)

    big = [x for x in ev if x[2] >= a.redraw_uah]
    small = [x for x in ev if x[2] < a.redraw_uah]
    if big:
        ax2.scatter([loc(t) for t, *_ in big], [x[2] for x in big], s=36, color=blue,
                    marker="o", edgecolors="#fcfcfb", linewidths=1, zorder=3,
                    label=f"teuer (≥ {a.redraw_uah:.0f} µAh): {len(big)}")
    if small:
        ax2.scatter([loc(t) for t, *_ in small], [x[2] for x in small], s=40, color=orange,
                    marker="^", edgecolors="#fcfcfb", linewidths=1, zorder=3,
                    label=f"günstig: {len(small)}")
    ax2.axhline(a.redraw_uah, color=muted, linewidth=0.8, linestyle="--")
    ax2.set_ylim(0, max([x[2] for x in ev] + [a.redraw_uah]) * 1.2)
    ax2.set_ylabel("µAh")
    ax2.set_title(f"Ladung pro Wachphase ({len(ev)} Stück, zusammen {w_uah / 1000:.2f} mAh)",
                  loc="left", color=ink)
    if ev:
        ax2.legend(loc="lower right", bbox_to_anchor=(1, 1), ncol=2, frameon=False, borderaxespad=0.2)

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m.\n%H:%M"))
    ax2.set_xlabel(f"Ortszeit (UTC{a.tz:+g})")
    fig.suptitle(f"{loc(rows[0][0]):%d.%m. %H:%M} bis {loc(rows[-1][0]):%d.%m. %H:%M}: "
                 f"{per_day:.1f} mAh pro Tag, ca. {a.mah * a.nutzbar / per_day:.0f} Tage Laufzeit"
                 if per_day > 0 else "", x=0.01, ha="left", color=ink)
    fig.tight_layout()
    try:
        fig.savefig(path, dpi=120, facecolor=fig.get_facecolor())
    except OSError as e:
        # z. B. data/ gehört dem Container (root): dann woanders hin schreiben
        sys.exit(f"\nDiagramm nicht geschrieben: {path}: {e.strerror}. "
                 f"Anderen Pfad wählen, z. B. --png ~/verlauf.png")
    print(f"\nDiagramm nach {path} geschrieben")


if a.png:
    plot(a.png)

if a.json:
    def ep(t):
        return round(t.timestamp(), 3)
    tzd = timedelta(hours=a.tz)
    # Tagesübersicht (Ortszeit)
    days = {}
    for t, m, d in iv:
        k = loc(t).date()
        x = days.setdefault(k, {"s": 0.0, "uAh": 0.0, "q_s": 0.0, "q_uAh": 0.0,
                                "wakes": 0, "wake_uAh": 0.0})
        x["s"] += d; x["uAh"] += m * d / 3600
        if m < a.aktiv:
            x["q_s"] += d; x["q_uAh"] += m * d / 3600
    for t, dur, uah, pk in ev:
        x = days.get(loc(t).date())
        if x:
            x["wakes"] += 1; x["wake_uAh"] += uah
    daily = [{"day": k.isoformat(), "hours": round(x["s"] / 3600, 3),
              "uAh": round(x["uAh"], 2), "wake_uAh": round(x["wake_uAh"], 2),
              "wakes": x["wakes"],
              "quiet_uA": round(x["q_uAh"] * 3600 / x["q_s"], 2) if x["q_s"] else None,
              "per_day_mAh": round(x["uAh"] / x["s"] * 86400 / 1000, 3) if x["s"] else None}
             for k, x in sorted(days.items())]
    # Verlauf verdichtet auf höchstens ca. 4000 Punkte (Mittel und Max je Bucket)
    span = (rows[-1][0] - rows[0][0]).total_seconds()
    bucket = max(60, int(math.ceil(span / 4000 / 60)) * 60)
    t_first = rows[0][0].timestamp()
    agg, mxs = {}, {}
    for (t, m, d), (_, _, mx) in zip(iv, rows[1:]):
        b = int((t.timestamp() - t_first) // bucket)
        x = agg.setdefault(b, [0.0, 0.0]); x[0] += m * d; x[1] += d
        mxs[b] = max(mxs.get(b, 0.0), mx)
    series = [[round(t_first + b * bucket, 3), round(x / y, 2) if y else None, round(mxs[b], 1)]
              for b, (x, y) in sorted(agg.items())]
    out = {
        "start": ep(rows[0][0]), "end": ep(rows[-1][0]), "measured_h": round(tot_s / 3600, 4),
        "params": {"aktiv_uA": a.aktiv, "redraw_uAh": a.redraw_uah, "mah": a.mah,
                   "nutzbar": a.nutzbar, "tz": a.tz},
        "quiet": {"mean_uA": round(q_mean, 2), "share": round(q_s / tot_s, 5) if tot_s else 0,
                  "median_uA": round(ms[len(ms) // 2], 2) if ms else None,
                  "p5_uA": round(ms[int(len(ms) * 0.05)], 2) if ms else None,
                  "p95_uA": round(ms[int(len(ms) * 0.95)], 2) if ms else None},
        "hourly": [[ep(k - tzd), round(v, 2)] for k, v in hourly],
        "wakes": [[ep(t), round(dur, 2), round(uah, 3), round(pk, 1)] for t, dur, uah, pk in ev],
        "total_uAh": round(tot_uah, 3), "avg_uA": round(tot_uah * 3600 / tot_s, 3),
        "wake_uAh": round(w_uah, 3), "per_day_mAh": round(per_day, 4),
        "days": round(a.mah * a.nutzbar / per_day, 1) if per_day > 0 else None,
        "daily": daily, "series_bucket_s": bucket, "series": series,
    }
    print(json.dumps(out))
