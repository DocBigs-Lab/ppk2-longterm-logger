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


def ha_sensors():
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
    if WAKE_CAPTURE:
        sensors += [
            ("wake_last_uAh", "Letzte Wachphase Ladung", "µAh", "measurement"),
            ("wake_last_s", "Letzte Wachphase Dauer", "s", "measurement"),
            ("wake_count", "Wachphasen seit Start", None, "measurement"),
        ]
    return sensors


def make_recorder():
    """WakeRecorder anlegen, falls eingeschaltet. Fehler hier dürfen die Messung nicht stoppen."""
    if not WAKE_CAPTURE:
        return None
    try:
        from wake_capture import WakeRecorder
        rec = WakeRecorder(OUT_DIR, trigger_ua=WAKE_TRIGGER_UA, trigger_ms=WAKE_TRIGGER_MS,
                           pre_s=WAKE_PRE_S, post_s=WAKE_POST_S, max_s=WAKE_MAX_S,
                           max_mb=WAKE_MAX_MB, min_free_mb=WAKE_MIN_FREE_MB,
                           rate=int(round(1 / SAMPLE_DT)),
                           meta={"mode": MODE, "voltage_mV": VOLTAGE_MV})
        print(f"Wachphasen-Aufnahme an (Schwelle {WAKE_TRIGGER_UA:g} µA über "
              f"{WAKE_TRIGGER_MS:g} ms, "
              f"Vorlauf {WAKE_PRE_S:g} s, Nachlauf {WAKE_POST_S:g} s, max. {WAKE_MAX_S:g} s, "
              f"Grenze {WAKE_MAX_MB:g} MB)", flush=True)
        return rec
    except Exception as e:
        print(f"Wachphasen-Aufnahme nicht verfügbar: {e!r}", file=sys.stderr, flush=True)
        return None


def make_live():
    """LiveFeed und Webserver starten, falls eingeschaltet. Fehler stoppen die Messung nicht."""
    if not LIVE_PORT:
        return None, None
    try:
        from live_view import LiveFeed, LiveServer
        feed = LiveFeed(history_s=LIVE_HISTORY_S, rate=int(round(1 / SAMPLE_DT)))
        server = LiveServer(feed, LIVE_PORT, info={"mode": MODE, "voltage_mV": VOLTAGE_MV},
                            data_dir=OUT_DIR)
        print(f"Live-Ansicht an: http://<Host>:{server.port}/ "
              f"({LIVE_HISTORY_S:g} s Verlauf)", flush=True)
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
        # Home-Assistant-Discovery: Sensoren erscheinen automatisch
        for key, name, unit, sclass in ha_sensors():
            cfg = {
                "name": name,
                "unique_id": f"ppk2_{key}",
                "state_topic": MQTT_TOPIC,
                "value_template": f"{{{{ value_json.{key} }}}}",
                "state_class": sclass,
                "device": {"identifiers": ["ppk2_logger"], "name": "PPK2 Logger",
                           "manufacturer": "Nordic Semiconductor", "model": "PPK2"},
            }
            if unit:
                cfg["unit_of_measurement"] = unit
            client.publish(f"homeassistant/sensor/ppk2_{key}/config",
                           json.dumps(cfg), retain=True)

    c.on_connect = on_connect
    c.connect_async(MQTT_HOST, MQTT_PORT)
    c.loop_start()
    return c


CSV_HEADER = ["timestamp_utc", "mean_uA", "min_uA", "max_uA", "samples",
              "charge_uAh_total", "median_uA"]


def open_csv():
    path = os.path.join(OUT_DIR, f"ppk2_{datetime.now():%Y-%m-%d}.csv")
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


def measure(mq, live=None):
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
    path, f, w = open_csv()
    quiet_hist = deque()   # (Zeit, Mittelwert, Dauer) ruhiger Intervalle
    recorder = make_recorder()
    try:
        while running:
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
                if mean_now < QUIET_MAX_UA:
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
                    path, f, w = open_csv()
                w.writerow(row)
                f.flush()
                if mq or live:
                    runtime_s = max(now - t_start, 1.0)
                    payload = dict(zip(
                        ["ts", "mean_uA", "min_uA", "max_uA", "samples", "charge_uAh",
                         "median_uA"], row))
                    payload["runtime_h"] = round(runtime_s / 3600.0, 3)
                    avg_total = charge * 3600.0 / runtime_s
                    payload["avg_total_uA"] = round(avg_total, 3)
                    if quiet_avg is not None:
                        payload["quiet_avg_uA"] = round(quiet_avg, 1)
                    if avg_total > 0:
                        payload["est_days"] = round(
                            BATTERY_MAH * 1000.0 * BATTERY_USABLE / avg_total / 24.0, 1)
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
                                      battery_mAh=BATTERY_MAH, battery_usable=BATTERY_USABLE,
                                      wake_capture=bool(recorder),
                                      wake_trigger_uA=WAKE_TRIGGER_UA,
                                      wake_trigger_ms=WAKE_TRIGGER_MS)
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
        if recorder:
            try:
                recorder.close(timeout=8)   # Docker wartet beim Stoppen 10 s
            except Exception:
                pass


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    mq = make_mqtt()
    live, _server = make_live()   # Server läuft über Neustarts der Messung hinweg
    while running:
        try:
            measure(mq, live)
        except Exception as e:  # Gerät weg, USB-Hänger usw. -> neu versuchen
            print(f"Fehler: {e!r} - neuer Versuch in 5 s", file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
