#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""PPK2 Langzeit-Logger: verdichtet 100 kSps auf Intervall-Statistik (CSV, optional MQTT)."""
import csv
import json
import os
import signal
import sys
import time
from collections import deque
from datetime import datetime, timezone

import numpy as np
from ppk2_api.ppk2_api import PPK2_API, PPK2_MP

from ppk2_fast import FastDecoder
from projects import FixedDir, ProjectStore

PORT = os.getenv("PPK2_PORT", "")                 # leer = automatisch suchen
MODE = os.getenv("PPK2_MODE", "source")           # source | ampere
VOLTAGE_MV = int(os.getenv("PPK2_VOLTAGE_MV", "3300"))
INTERVAL = float(os.getenv("LOG_INTERVAL_S", "1"))
OUT_DIR = os.getenv("OUT_DIR", "/data")
SAMPLE_DT = 1e-5                                  # 100 kSps -> 10 µs pro Sample

MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "ppk2/stats")

BATTERY_MAH = float(os.getenv("BATTERY_MAH", "3000"))        # Akkukapazität
BATTERY_USABLE = float(os.getenv("BATTERY_USABLE", "0.8"))   # nutzbarer Anteil (0..1)

# Ruhestrom: gleitender Mittelwert nur über "ruhige" Intervalle (Wachphasen zählen nicht mit)
ROLL_WINDOW_S = float(os.getenv("ROLL_WINDOW_S", "300"))     # Fensterbreite in Sekunden
QUIET_MAX_UA = float(os.getenv("QUIET_MAX_UA", "5000"))      # Intervalle darüber gelten als aktiv

# Wachphasen in voller Auflösung speichern (wake_capture.py), standardmäßig aus
WAKE_CAPTURE = os.getenv("WAKE_CAPTURE", "0").strip().lower() in ("1", "true", "yes", "on")
WAKE_TRIGGER_UA = float(os.getenv("WAKE_TRIGGER_UA", "") or QUIET_MAX_UA)
WAKE_TRIGGER_MS = float(os.getenv("WAKE_TRIGGER_MS", "100"))
WAKE_PRE_S = float(os.getenv("WAKE_PRE_S", "2"))
WAKE_POST_S = float(os.getenv("WAKE_POST_S", "2"))
WAKE_MAX_S = float(os.getenv("WAKE_MAX_S", "120"))
WAKE_MAX_MB = float(os.getenv("WAKE_MAX_MB", "2000"))
WAKE_MIN_FREE_MB = float(os.getenv("WAKE_MIN_FREE_MB", "500"))

# Live-Ansicht im Browser (live_view.py), leer oder 0 = aus
LIVE_PORT = int(os.getenv("LIVE_PORT", "") or 0)
LIVE_HISTORY_S = float(os.getenv("LIVE_HISTORY_S", "120"))
LIVE_VERLAUF_H = float(os.getenv("LIVE_VERLAUF_H", "24"))
# Schutz für Änderungen über die Weboberfläche (Projekte), leer = keiner
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")


def env_settings():
    """Einstellungen aus .env: Vorbelegung neuer Projekte. Danach gelten die Werte im
    Projekt (projekt.json, über die Weboberfläche änderbar)."""
    return {"battery_mAh": BATTERY_MAH, "battery_usable": BATTERY_USABLE,
            "quiet_max_uA": QUIET_MAX_UA, "redraw_uAh": 120.0,
            "wake_capture": WAKE_CAPTURE, "wake_trigger_uA": WAKE_TRIGGER_UA,
            "wake_trigger_ms": WAKE_TRIGGER_MS, "wake_pre_s": WAKE_PRE_S,
            "wake_post_s": WAKE_POST_S, "wake_max_s": WAKE_MAX_S, "wake_max_mb": WAKE_MAX_MB}


WAKE_KEYS = ("wake_capture", "wake_trigger_uA", "wake_trigger_ms", "wake_pre_s",
             "wake_post_s", "wake_max_s", "wake_max_mb")

running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def find_port():
    if PORT:
        return PORT
    # Das PPK2 legt zwei Schnittstellen an: die niedrigere (z. B. ttyACM0) ist der
    # Messdatenkanal, die höhere eine Shell-Konsole. Die Bibliothek liefert die Reihenfolge
    # nicht garantiert, daher sortieren.
    devs = sorted(PPK2_API.list_devices())
    if not devs:
        raise RuntimeError("Kein PPK2 gefunden")
    return devs[0]


def ha_sensors(wake=None):
    if wake is None:
        wake = WAKE_CAPTURE
    sensors = [
        ("mean_uA", "Strom Mittel", "µA", "measurement"),
        ("quiet_avg_uA", f"Ruhestrom ({ROLL_WINDOW_S / 60:g} min)", "µA", "measurement"),
        ("median_uA", "Strom Median", "µA", "measurement"),
        ("min_uA", "Strom Min", "µA", "measurement"),
        ("max_uA", "Strom Max", "µA", "measurement"),
        ("charge_uAh", "Ladung gesamt", "µAh", "total_increasing"),
        ("avg_total_uA", "Strom Durchschnitt seit Start", "µA", "measurement"),
        ("runtime_h", "Laufzeit", "h", "measurement"),
        ("est_days", "Geschätzte Akkulaufzeit", "d", "measurement"),
    ]
    if wake:
        sensors += WAKE_SENSORS
    sensors.append(("projekt", "Projekt", None, None))
    return sensors


WAKE_SENSORS = [
    ("wake_last_uAh", "Letzte Wachphase Ladung", "µAh", "measurement"),
    ("wake_last_s", "Letzte Wachphase Dauer", "s", "measurement"),
    ("wake_count", "Wachphasen seit Start", None, "measurement"),
]


def make_recorder(out_dir=None, s=None):
    """WakeRecorder anlegen, falls eingeschaltet. Fehler hier dürfen die Messung nicht stoppen."""
    s = s or env_settings()
    if not s["wake_capture"]:
        return None
    try:
        from wake_capture import WakeRecorder
        rec = WakeRecorder(out_dir or OUT_DIR, trigger_ua=s["wake_trigger_uA"],
                           trigger_ms=s["wake_trigger_ms"],
                           pre_s=s["wake_pre_s"], post_s=s["wake_post_s"],
                           max_s=s["wake_max_s"], max_mb=s["wake_max_mb"],
                           min_free_mb=WAKE_MIN_FREE_MB,
                           rate=int(round(1 / SAMPLE_DT)),
                           meta={"mode": MODE, "voltage_mV": VOLTAGE_MV})
        print(f"Wachphasen-Aufnahme an (Schwelle {s['wake_trigger_uA']:g} µA über "
              f"{s['wake_trigger_ms']:g} ms, Vorlauf {s['wake_pre_s']:g} s, "
              f"Nachlauf {s['wake_post_s']:g} s, max. {s['wake_max_s']:g} s, "
              f"Grenze {s['wake_max_mb']:g} MB)", flush=True)
        return rec
    except Exception as e:
        print(f"Wachphasen-Aufnahme nicht verfügbar: {e!r}", file=sys.stderr, flush=True)
        return None


def make_live(projects=None):
    """LiveFeed und Webserver starten, falls eingeschaltet. Fehler stoppen die Messung nicht."""
    if not LIVE_PORT:
        return None, None
    try:
        from live_view import LiveFeed, LiveServer
        feed = LiveFeed(history_s=LIVE_HISTORY_S, rate=int(round(1 / SAMPLE_DT)),
                        long_s=LIVE_VERLAUF_H * 3600)
        server = LiveServer(feed, LIVE_PORT, info={"mode": MODE, "voltage_mV": VOLTAGE_MV},
                            data_dir=OUT_DIR, projects=projects, token=ADMIN_TOKEN)
        print(f"Live-Ansicht an: http://<Host>:{server.port}/ "
              f"({LIVE_HISTORY_S:g} s fein, {LIVE_VERLAUF_H:g} h grob)", flush=True)
        return feed, server
    except Exception as e:
        print(f"Live-Ansicht nicht verfügbar: {e!r}", file=sys.stderr, flush=True)
        return None, None


def make_mqtt():
    if not MQTT_HOST:
        return None
    import paho.mqtt.client as mqtt
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:  # paho < 2
        c = mqtt.Client()
    if MQTT_USER:
        c.username_pw_set(MQTT_USER, MQTT_PASS)

    def on_connect(client, *args):
        publish_discovery(client)

    c.on_connect = on_connect
    c.connect_async(MQTT_HOST, MQTT_PORT)
    c.loop_start()
    return c


_discovery_wake = None   # Stand der Wachphasen-Sensoren (aktives Projekt), None = .env


def publish_discovery(client, wake=None):
    """Home-Assistant-Discovery: Sensoren erscheinen automatisch. Ist die
    Wachphasen-Aufnahme im aktiven Projekt aus, werden deren Sensoren entfernt."""
    global _discovery_wake
    if wake is not None:
        _discovery_wake = wake
    wake = WAKE_CAPTURE if _discovery_wake is None else _discovery_wake
    for key, name, unit, sclass in ha_sensors(wake):
        cfg = {
            "name": name,
            "unique_id": f"ppk2_{key}",
            "state_topic": MQTT_TOPIC,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "device": {"identifiers": ["ppk2_logger"], "name": "PPK2 Logger",
                       "manufacturer": "Nordic Semiconductor", "model": "PPK2"},
        }
        if sclass:
            cfg["state_class"] = sclass
        if unit:
            cfg["unit_of_measurement"] = unit
        client.publish(f"homeassistant/sensor/ppk2_{key}/config",
                       json.dumps(cfg), retain=True)
    if not wake:
        for key, *_ in WAKE_SENSORS:
            client.publish(f"homeassistant/sensor/ppk2_{key}/config", "", retain=True)


CSV_HEADER = ["timestamp_utc", "mean_uA", "min_uA", "max_uA", "samples",
              "charge_uAh_total", "median_uA"]


def open_csv(out_dir=None):
    path = os.path.join(out_dir or OUT_DIR, f"ppk2_{datetime.now():%Y-%m-%d}.csv")
    if os.path.exists(path):
        # Datei mit altem Spaltenlayout beiseite legen, damit die Spalten nicht gemischt werden
        with open(path, newline="") as old:
            first = old.readline().strip()
        if first != ",".join(CSV_HEADER):
            os.rename(path, path + f".alt-{datetime.now():%H%M%S}")
    new = not os.path.exists(path)
    f = open(path, "a", newline="")
    w = csv.writer(f)
    if new:
        w.writerow(CSV_HEADER)
    return path, f, w


def quiesce(ppk2):
    """Laufenden Datenstrom stoppen und restliche Bytes verwerfen, bis Ruhe herrscht."""
    for _ in range(6):
        try:
            ppk2.stop_measuring()
        except Exception:
            pass
        time.sleep(0.3)
        t_end = time.time() + 3
        while ppk2.ser.in_waiting and time.time() < t_end:
            ppk2.ser.read(ppk2.ser.in_waiting)
            time.sleep(0.05)
        time.sleep(0.3)
        if not ppk2.ser.in_waiting:
            ppk2.ser.reset_input_buffer()
            return True
    return False


def close_recorder(recorder):
    if recorder:
        try:
            recorder.close(timeout=8)   # Docker wartet beim Stoppen 10 s
        except Exception:
            pass


def measure(mq, live=None, projects=None):
    """Messschleife. projects: ProjectStore (Ordner und Einstellungen des aktiven Projekts,
    Wechsel ohne Neustart). Ohne: OUT_DIR und Einstellungen aus .env."""
    projects = projects or FixedDir(OUT_DIR, env_settings())
    gen, cur = projects.snapshot()
    s = cur["settings"]
    port = find_port()
    print(f"Verwende Port {port}", flush=True)
    # PPK2_MP liest die Rohdaten in einem eigenen Thread, damit der Empfangspuffer beim
    # Dekodieren nicht überläuft (sonst gehen Samples verloren).
    ppk2 = PPK2_MP(port)
    # Erst normal die Kalibrierdaten lesen. Streamt das PPK2 noch aus einem vorherigen Lauf,
    # kommen Binärdaten statt Metadaten: dann stoppen, Puffer leeren und nochmal versuchen.
    try:
        ok = ppk2.get_modifiers()
    except Exception:
        ok = None
    if not ok:
        quiet = quiesce(ppk2)
        print(f"Alter Datenstrom gestoppt: {quiet}", flush=True)
        ok = ppk2.get_modifiers()
    print(f"Kalibrierdaten geladen: {ok}", flush=True)
    if MODE == "ampere":
        ppk2.use_ampere_meter()
        # Die Bibliothek prüft in start_measuring() nur, ob ein Spannungswert gesetzt ist.
        # Nur die Variable setzen, KEINEN Befehl ans Gerät schicken (kein Eingriff in den Regler).
        ppk2.current_vdd = VOLTAGE_MV
        # Gerät auf "running" setzen (wie die Nordic-App beim Start): ohne das kein
        # Datenstrom und kein Durchgang zum Testobjekt
        ppk2.toggle_DUT_power("ON")
    else:
        ppk2.use_source_meter()
        ppk2.set_source_voltage(VOLTAGE_MV)
        ppk2.toggle_DUT_power("ON")
    ppk2.start_measuring()
    print(f"Messung gestartet ({MODE}, {VOLTAGE_MV} mV)", flush=True)

    decoder = FastDecoder(ppk2)
    buf, n, t0, charge = [], 0, time.time(), 0.0
    t_start = t0
    day = datetime.now().date()
    path, f, w = open_csv(cur["dir"])
    quiet_hist = deque()   # (Zeit, Mittelwert, Dauer) ruhiger Intervalle
    recorder = make_recorder(cur["dir"], s)
    if cur["name"]:
        print(f"Projekt: {cur['name']} ({cur['slug']})", flush=True)
    try:
        while running:
            if projects.gen != gen:
                # Projekt gewechselt oder Einstellungen geändert: umstellen, ohne die
                # serielle Verbindung anzufassen (der Prüfling bleibt versorgt)
                gen, new = projects.snapshot()
                if new["slug"] != cur["slug"]:
                    close_recorder(recorder)
                    f.close()
                    cur, s = new, new["settings"]
                    path, f, w = open_csv(cur["dir"])
                    day = datetime.now().date()
                    recorder = make_recorder(cur["dir"], s)
                    now = time.time()
                    buf, n, t0, t_start, charge = [], 0, now, now, 0.0
                    quiet_hist.clear()
                    print(f"Projekt gewechselt: {cur['name']} ({cur['slug']}), "
                          f"Zähler auf 0", flush=True)
                else:
                    old, cur, s = s, new, new["settings"]
                    if any(old[k] != s[k] for k in WAKE_KEYS):
                        close_recorder(recorder)
                        recorder = make_recorder(cur["dir"], s)
                    print(f"Einstellungen von {cur['name']} übernommen", flush=True)
                if mq:
                    try:
                        publish_discovery(mq, s["wake_capture"])
                    except Exception as e:
                        print(f"MQTT-Discovery fehlgeschlagen: {e!r}", file=sys.stderr,
                              flush=True)
            raw = ppk2.get_data()
            if raw:
                samples = decoder.decode(raw)
                if len(samples):
                    buf.append(samples)
                    n += len(samples)
                    if recorder:
                        try:
                            recorder.feed(samples, time.time())
                        except Exception as e:
                            # Nur die Aufnahme abschalten, die Messung läuft weiter
                            print(f"Wachphasen-Aufnahme abgeschaltet: {e!r}",
                                  file=sys.stderr, flush=True)
                            recorder = None
                    if live:
                        try:
                            live.feed(samples, time.time())
                        except Exception as e:
                            print(f"Live-Ansicht abgeschaltet: {e!r}",
                                  file=sys.stderr, flush=True)
                            live = None
            now = time.time()
            if now - t0 >= INTERVAL and buf:
                a = np.concatenate(buf)
                # Ladung aus Mittelwert x echter Intervalldauer: unabhängig von verlorenen Samples
                mean_now = float(a.mean())
                charge += mean_now * (now - t0) / 3600.0
                if mean_now < s["quiet_max_uA"]:
                    quiet_hist.append((now, mean_now, now - t0))
                while quiet_hist and now - quiet_hist[0][0] > ROLL_WINDOW_S:
                    quiet_hist.popleft()
                quiet_dt = sum(d for _, _, d in quiet_hist)
                quiet_avg = (sum(m * d for _, m, d in quiet_hist) / quiet_dt
                             if quiet_dt >= min(30.0, ROLL_WINDOW_S / 2) else None)
                row = [datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       round(float(a.mean()), 3), round(float(a.min()), 3),
                       round(float(a.max()), 3), len(a), round(charge, 4),
                       round(float(np.median(a)), 3)]
                if datetime.now().date() != day:
                    f.close()
                    day = datetime.now().date()
                    path, f, w = open_csv(cur["dir"])
                w.writerow(row)
                f.flush()
                if mq or live:
                    runtime_s = max(now - t_start, 1.0)
                    payload = dict(zip(
                        ["ts", "mean_uA", "min_uA", "max_uA", "samples", "charge_uAh",
                         "median_uA"], row))
                    payload["runtime_h"] = round(runtime_s / 3600.0, 3)
                    if cur["name"]:
                        payload["projekt"] = cur["name"]
                    avg_total = charge * 3600.0 / runtime_s
                    payload["avg_total_uA"] = round(avg_total, 3)
                    if quiet_avg is not None:
                        payload["quiet_avg_uA"] = round(quiet_avg, 1)
                    if avg_total > 0:
                        payload["est_days"] = round(
                            s["battery_mAh"] * 1000.0 * s["battery_usable"]
                            / avg_total / 24.0, 1)
                    if recorder:
                        payload["wake_count"] = recorder.count
                        if recorder.last:
                            payload["wake_last_uAh"] = round(recorder.last["active_uAh"], 1)
                            payload["wake_last_s"] = round(recorder.last["active_s"], 2)
                    if mq:
                        mq.publish(MQTT_TOPIC, json.dumps(payload))
                    if live:
                        try:
                            st = dict(payload, interval_s=round(now - t0, 3),
                                      quiet_window_s=ROLL_WINDOW_S,
                                      battery_mAh=s["battery_mAh"],
                                      battery_usable=s["battery_usable"],
                                      quiet_max_uA=s["quiet_max_uA"],
                                      wake_capture=bool(recorder),
                                      wake_trigger_uA=s["wake_trigger_uA"],
                                      wake_trigger_ms=s["wake_trigger_ms"],
                                      projekt_slug=cur["slug"])
                            if recorder:
                                st["wake_rec"] = recorder._state == "rec"
                                if st["wake_rec"]:
                                    st["wake_since"] = recorder._t_trigger
                            live.set_status(st)
                        except Exception as e:
                            print(f"Live-Ansicht abgeschaltet: {e!r}",
                                  file=sys.stderr, flush=True)
                            live = None
                buf, t0 = [], now
            time.sleep(0.005)
    finally:
        try:
            ppk2.stop_measuring()
            if MODE != "ampere":
                ppk2.toggle_DUT_power("OFF")
        except Exception:
            pass
        f.close()
        close_recorder(recorder)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    projects = ProjectStore(OUT_DIR, env_settings(),
                            fixed={"mode": MODE, "voltage_mV": VOLTAGE_MV})
    global _discovery_wake
    _discovery_wake = projects.snapshot()[1]["settings"]["wake_capture"]
    mq = make_mqtt()
    live, _server = make_live(projects)   # Server läuft über Neustarts der Messung hinweg
    while running:
        try:
            measure(mq, live, projects)
        except Exception as e:  # Gerät weg, USB-Hänger usw. -> neu versuchen
            print(f"Fehler: {e!r} - neuer Versuch in 5 s", file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
