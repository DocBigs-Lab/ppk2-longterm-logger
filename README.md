<p align="center">
  <a href="https://docbigs-lab.de">
    <img src="docs/docbigslab_logo.svg" alt="DocBigsLab Logo" width="200">
  </a>
</p>
<p align="center">
  <a href="https://docbigs-lab.de">https://docbigs-lab.de</a>
</p>

# PPK2 Longterm Logger

Langzeitmessungen mit dem **Nordic Power Profiler Kit II (PPK2)** auf einem Raspberry Pi, ohne dass ein PC mitlaufen muss. Läuft als Docker-Container, schreibt Statistik-CSVs, meldet den Stand an **Home Assistant** (MQTT) und zeigt den Strom auf Wunsch live im Browser.

*English in short: Docker-based long-term logger for the Nordic PPK2 on a Raspberry Pi. Condenses 100 kSps into per-interval statistics (mean/min/max/median/charge), writes daily CSV files, optionally publishes to Home Assistant via MQTT discovery (incl. a rolling quiet-current sensor and battery-life estimate). Optional: every wake-up saved at full 100 kSps, and a live browser view (1 ms min/max/mean envelope, measure/zoom, oscilloscope-style trigger, CSV/PNG export) while logging continues. Includes analysis scripts for sleep current, wake-ups and battery lifetime. README is in German.*

Entstanden beim Messen eines ESP32-C6-E-Paper-Displays im Deep Sleep, aber nicht darauf beschränkt: alles, was Strom zieht und sich über Tage beobachten lassen soll.

## Was es kann

- **Dauerbetrieb über Tage**: 100.000 Samples pro Sekunde werden fortlaufend zu einem Datensatz pro Intervall verdichtet (Mittel, Min, Max, Median, Samplezahl, Gesamtladung). Das Dekodieren läuft in numpy, einschließlich des Spike-Filters der Bibliothek. Auf einem Raspberry Pi 4 kommen die vollen 100 kSps lückenlos an, auch wenn das PPK2 über 10 000-mal pro Sekunde den Messbereich wechselt.
- **PPK2 automatisch finden**: Das PPK2 legt zwei serielle Ports an. Der Logger nimmt den Datenport und ignoriert die Shell, auch wenn sich die Nummern nach dem Umstecken ändern.
- **Stream-Wiederherstellung**: Läuft das PPK2 nach einem Absturz oder Neustart noch im Messmodus, wird der alte Datenstrom gestoppt und die Kalibrierung sauber geladen.
- **Ampere- und Source-Modus**: externe Versorgung durchmessen oder das Testobjekt direkt vom PPK2 versorgen.
- **Home Assistant per MQTT-Discovery**: die Sensoren legen sich selbst an, ohne YAML.
- **Akkulaufzeit-Schätzung** aus dem Durchschnitt seit Projektstart, in der Live-Ansicht auch für einen frei gewählten Bereich.
- **Ruhestrom-Sensor**: gleitender Mittelwert nur über „ruhige“ Intervalle, damit Wachphasen den Wert nicht verfälschen. Ideal, um Hardware-Umbauten live vorher/nachher zu vergleichen.
- **`analyse.py`**: wertet die CSVs aus (Ruhestrom pro Stunde, Wachphasen, Tagesbilanz, Laufzeit), auf Wunsch als Diagramm.
- **Wachphasen in voller Auflösung** (optional): Jede Wachphase wird mit 100 kSps samt Vorlauf gespeichert, `wake_view.py` zeigt Verlauf, Spitzen und Ladung (siehe [unten](#wachphasen-in-voller-auflösung)).
- **Live-Ansicht im Browser** (optional): Hüllkurve mit 1 ms Auflösung, Fenster bis 24 h, Marker, Bereiche ausmessen, Trigger wie am Oszilloskop, Export als CSV und PNG, während der Logger weiter misst (siehe [unten](#live-ansicht-im-browser)).
- **Auswertung im Browser** (mit der Live-Ansicht): Zeitraum wählen, Ruhestrom, Wachphasen, Tagesbilanz und Laufzeit als Diagramme, jede Wachphase bis auf einzelne Samples zoomen (siehe [unten](#auswertung-im-browser)).
- **Projekte** (mit der Live-Ansicht): jedes Board bekommt einen eigenen Datenordner. Ein Assistent im Browser legt ein neues Projekt an und archiviert das alte, ohne die Messung zu unterbrechen. Archivierte Projekte lassen sich auswerten oder fortsetzen (siehe [unten](#projekte)).
- **`diag.py`**: prüft, ob alle Samples ankommen, und schneidet Rohdaten mit (siehe [Diagnose](#diagnose)).

## Aufbau

Beispiel für den Ampere-Modus mit Akku (der Prüfling wird durch das PPK2 „durchgereicht“):

```
Akku (+) ──► PPK2 VIN        PPK2 VOUT ──► Prüfling (+)
Akku (−) ──► PPK2 GND ───────────────────► Prüfling (−)
                   │
                   └─ USB ──► Raspberry Pi (Docker)
```

Im Source-Modus (`PPK2_MODE=source`) versorgt das PPK2 den Prüfling selbst mit der eingestellten Spannung.

## Schnellstart

Voraussetzung: Raspberry Pi mit Docker und Docker Compose, PPK2 per USB.

```bash
git clone https://github.com/DocBigs-Lab/ppk2-longterm-logger.git
cd ppk2-longterm-logger
cp .env.example .env      # anpassen, vor allem PPK2_MODE und optional MQTT
docker compose up -d
docker logs -f ppk2-logger
```

Im Log siehst du den verwendeten Port, „Kalibrierdaten geladen: True“ und „Messung gestartet“. Die Daten landen in `./data/ppk2_JJJJ-MM-TT.csv` (ein Tag pro Datei, Zeitstempel in UTC).

Statt des fertigen Images kannst du auch selbst bauen: in `docker-compose.yml` die Zeile `image` auskommentieren, `build: .` einkommentieren und mit `docker compose up -d --build` starten. Nach jeder Änderung an den Skripten ist ein Neubau nötig (`--build --force-recreate`).

## Konfiguration (`.env`)

| Variable | Standard | Bedeutung |
|---|---|---|
| `PPK2_MODE` | `source` | `source` = PPK2 versorgt den Prüfling, `ampere` = nur Strommessung |
| `PPK2_VOLTAGE_MV` | `3300` | Spannung in mV, im Ampere-Modus der Wert für die Berechnung |
| `PPK2_PORT` | leer | leer = automatisch suchen, sonst z. B. `/dev/ttyACM0` |
| `LOG_INTERVAL_S` | `1` | Intervall der Statistik in Sekunden |
| `OUT_DIR` | `/data` | Datenordner im Container, darin `projekte/<name>/` |
| `BATTERY_MAH` | `3000` | Akkukapazität für die Laufzeitschätzung |
| `BATTERY_USABLE` | `0.8` | nutzbarer Anteil der Kapazität |
| `MQTT_HOST` | leer | leer = MQTT aus |
| `MQTT_PORT`, `MQTT_USER`, `MQTT_PASS` | `1883`, leer, leer | Zugang zum Broker |
| `MQTT_TOPIC` | `ppk2/stats` | Topic der Zustandsmeldungen |
| `ROLL_WINDOW_S` | `300` | Fenster des Ruhestrom-Sensors in Sekunden |
| `QUIET_MAX_UA` | `5000` | Intervalle mit einem Mittelwert darüber zählen als „aktiv“ |
| `WAKE_CAPTURE` | `0` | `1` = Wachphasen in voller Auflösung speichern |
| `WAKE_TRIGGER_UA` | leer | Schwelle für das gleitende Mittel in µA, leer = `QUIET_MAX_UA` |
| `WAKE_TRIGGER_MS` | `100` | Fenster des gleitenden Mittels für den Trigger in ms |
| `WAKE_PRE_S`, `WAKE_POST_S` | `2`, `2` | Vorlauf und Nachlauf in Sekunden. Der Nachlauf ist auch die Ruhezeit, nach der eine Wachphase als beendet gilt |
| `WAKE_MAX_S` | `120` | Höchstdauer einer Aufnahme in Sekunden |
| `WAKE_MAX_MB` | `2000` | Obergrenze für `data/wake`, älteste Dateien werden gelöscht |
| `WAKE_MIN_FREE_MB` | `500` | darunter wird nicht mehr gespeichert |
| `LIVE_PORT` | leer | Port der Live-Ansicht und Auswertung im Container, leer = aus |
| `LIVE_HISTORY_S` | `120` | so viele Sekunden hält der Logger für die Live-Ansicht in 1 ms Auflösung vor (ca. 1,4 MB bei 120 s) |
| `LIVE_VERLAUF_H` | `24` | so viele Stunden hält er zusätzlich in 100 ms Auflösung vor (ca. 17 MB bei 24 h), für lange Fenster |
| `ADMIN_TOKEN` | leer | Passwort für Änderungen auf der Seite „Projekte“, leer = jeder im Heimnetz darf |

Akku (`BATTERY_*`), `QUIET_MAX_UA` und alle `WAKE_*`-Werte außer `WAKE_MIN_FREE_MB` sind nur die **Vorbelegung** für neue Projekte. Danach gelten die Werte im Projekt, änderbar auf der Seite „Projekte“ ohne Neustart (siehe [Projekte](#projekte)). Modus und Spannung bleiben bewusst in `.env`.

Kommentare in der `.env` gehören in eigene Zeilen. Docker liest sie sonst als Teil des Werts, aus `MQTT_TOPIC=ppk2/stats # Topic` wird ein ungültiges Topic.

## Home Assistant

Mit gesetztem `MQTT_HOST` meldet sich der Logger per Discovery als Gerät „PPK2 Logger“ und legt diese Sensoren an:

- Strom Mittel, Median, Min, Max (µA, pro Intervall)
- **Ruhestrom (5 min)** (µA, gleitend über ruhige Intervalle, braucht etwa 30 s Ruhe bis zum ersten Wert)
- Ladung gesamt (µAh)
- Strom Durchschnitt seit Start (µA)
- Laufzeit (h)
- Geschätzte Akkulaufzeit (Tage)
- mit Wachphasen-Aufnahme zusätzlich: Letzte Wachphase Ladung (µAh) und Dauer (s), Wachphasen seit Start
- Projekt (Name des aktiven Projekts)

Nach einem Projektwechsel beginnen Ladung, Durchschnitt und Laufzeit wieder bei 0. Die Sensoren bleiben dieselben, im Verlauf von Home Assistant liegen beide Projekte hintereinander.

![Anzeige in Home Assistant](ppk2-ha.jpg)

## Auswertung

Die CSV enthält pro Intervall `timestamp_utc, mean_uA, min_uA, max_uA, samples, charge_uAh_total, median_uA`. Für eine Zusammenfassung läuft `analyse.py` direkt auf dem Pi, nur mit Python 3 (dasselbe mit Diagrammen im Browser: [Auswertung im Browser](#auswertung-im-browser)):

```bash
python3 analyse.py                          # letzte 24 h
python3 analyse.py --hours 12
python3 analyse.py --von 2026-11-07T10:30   # ab Zeitpunkt (UTC) bis jetzt
python3 analyse.py --von 2026-11-07T20:00 --hours 10   # 10 h ab Zeitpunkt
python3 analyse.py --raster 1800            # Abweichung der Wachphasen von :00/:30
python3 analyse.py --hours 72 --png verlauf.png   # zusätzlich Diagramm
```

Ausgegeben werden Ruhestrom (Mittel und pro Stunde), eine Liste der Wachphasen mit Dauer und Ladung, die Tagesbilanz und die hochgerechnete Akkulaufzeit. Einstellbar sind unter anderem `--aktiv` (Schwelle für Wachphasen in µA), `--mah`, `--nutzbar`, `--tz` (Ortszeit-Offset) und `--redraw-uah` (ab welcher Ladung eine Wachphase als „teuer“ gilt).

`--png` zeichnet zwei Diagramme mit gemeinsamer Zeitachse: Ruhestrom pro Stunde und Ladung jeder Wachphase (teuer/günstig, Schwelle gestrichelt). Dafür braucht es matplotlib, auf dem Pi z. B. `sudo apt install python3-matplotlib`. Ohne `--png` bleibt es bei der Standardbibliothek. `data/` gehört dem Container (root), das Diagramm deshalb z. B. nach `~/verlauf.png` schreiben.

## Wachphasen in voller Auflösung

Mit `WAKE_CAPTURE=1` hält der Logger die letzten Sekunden in einem Ringpuffer. Steigt der über 100 ms gemittelte Strom über die Schwelle, wird aufgezeichnet, bis er `WAKE_POST_S` Sekunden lang darunter bleibt. Kurze Einbrüche, etwa zwischen WLAN und Display-Refresh, beenden die Aufnahme nicht.

Das lange Fenster ist Absicht: Der Ruhestrom vieler Schaltungen besteht aus kurzen Pulsen (Spannungswandler im Pulsbetrieb). Am ESP32-C6-Display erreichen sie im 1-ms-Mittel bis ca. 30 mA, im 100-ms-Mittel unter 1 mA. Eine Wachphase liegt dagegen auch im 100-ms-Mittel weit über 100 mA. Mit 1 ms löst jeder Puls aus und die Aufnahme endet nie.

- Dateien: `data/wake/JJJJ-MM-TT/wake_JJJJMMTTTHHMMSSZ.npz` (UTC), Inhalt `uA` (float32, 10 µs pro Sample) und `meta` (JSON mit Startzeit, Vorlauf, Schwelle, Ladung usw.)
- Übersicht: `data/wake/wake_index.csv`, eine Zeile pro Wachphase, auch für verworfene
- Größe: gemessen etwa 4:1 komprimiert, ca. 3 MB pro 30-s-Wachphase. Bei 18 Wachphasen am Tag sind das ca. 55 MB, 2000 MB reichen für gut einen Monat. Danach werden die ältesten Dateien gelöscht, der Index bleibt.
- Gespeichert wird in einem eigenen Thread. Ist er noch belegt oder der Platz knapp, wird die Wachphase verworfen und im Index vermerkt. Die Messung läuft in jedem Fall weiter, ein Fehler in der Aufnahme schaltet nur die Aufnahme ab.
- Ein Dauerstrom (z. B. Laden) wird nach `WAKE_MAX_S` abgeschnitten, danach wartet der Recorder, bis es wieder ruhig ist.

Auswertung mit `wake_view.py`. Liste und `--tage` laufen überall, Verlauf und `--csv` brauchen numpy. Am einfachsten im Container:

```bash
docker exec ppk2-logger python wake_view.py                   # letzte 20 Wachphasen
docker exec ppk2-logger python wake_view.py --tage 7          # Summe und Mittel pro Tag
docker exec ppk2-logger python wake_view.py letzte            # Verlauf der letzten Wachphase im 1-s-Raster
docker exec ppk2-logger python wake_view.py letzte --raster 0.2
docker exec ppk2-logger python wake_view.py 2026-11-09/wake_20261109T050012Z.npz --csv /data/phase.csv --bucket-ms 1
```

`--csv` schreibt Min/Mittel/Max pro Raster, `--png` ein Diagramm. Dafür braucht es matplotlib, das im Image fehlt: entweder auf dem PC (`python3 wake_view.py --dir ./data letzte --png phase.png`) oder vorübergehend im laufenden Container (`docker exec ppk2-logger pip install matplotlib`, kein Neustart, nach einem Neubau wieder weg).

Grenzen: Die Zeitstempel sind auf etwa 0,1 s genau (Blockgröße der Bibliothek). Der Recorder misst die effektive Rate seit Start (`rate_eff` in den Metadaten) und rechnet Dauer, Zeitachse und µAh damit. Vorlauf, Nachlauf und Höchstdauer sind in Samples bemessen. Die Werte enthalten den Spike-Filter der Bibliothek wie die Statistik auch. Aufnahmen aus älteren Versionen (vor dem neuen Dekoder, siehe [Grenzen](#grenzen)) haben nur ca. 88 kSps, `rate_eff` gleicht das aus.

## Live-Ansicht im Browser

Der Logger kann den Strom live im Browser zeigen, während er weiter misst und aufzeichnet. Er bleibt dabei einziger Besitzer des PPK2. Zu sehen ist je Millisekunde Min, Max und Mittel (Hüllkurve), linear oder logarithmisch, mit Uhrzeit-Achse. Das Fenster wählt man aus festen Werten (0,1 s bis 24 h) oder gibt es frei ein (z. B. „90 s“, „7 min“, „6 h“, auch als Adresse `/?fenster=15min`). Bis 2 Minuten zeigt die Seite 1-ms-Werte, darüber 100-ms-Werte aus dem Verlauf des Loggers (`LIVE_VERLAUF_H`, Standard 24 h). Die Seite lädt nur den Teil, der gerade gebraucht wird. War die Verbindung unterbrochen (z. B. Rechner im Ruhezustand), holt sie die fehlende Zeit danach beim Logger nach. Der Verlauf liegt nur im Speicher: Nach einem Neustart des Containers beginnt er leer, ältere Daten stehen in der [Auswertung](#auswertung-im-browser).

- **Ausmessen und Zoom**: Bereich mit der Maus ziehen zeigt Dauer, Mittel, Min, Spitze (1 ms), Ladung und „Akku ≙ N Tage“: so lange hielte der Akku des Projekts, wenn der Strom dauerhaft so wäre wie im Bereich. Aussagekräftig nur für Bereiche über ganze Zyklen (Schlaf plus Wachphasen), ohne Anlaufphase. Mausrad zoomt, Shift+Mausrad verschiebt, Doppelklick führt zurück zu live, „Zoom auf Auswahl“ vergrößert den markierten Bereich.
- **Marker**: Knopf „Marker“ setzt einen bei jetzt, Rechtsklick ins Diagramm an dieser Stelle. „→ jetzt“ zeigt live alles ab dem Marker (Fenster wächst mit, Mittel, Ladung und Akkulaufzeit seit dem Marker), „von/bis“ den Bereich zwischen zwei Markern. Name ändern per Klick auf den Namen. Marker sind nach Uhrzeit im Browser gespeichert (`localStorage`): Sie überstehen Neuladen und Neustarts, gelten aber nur in diesem Browser und unter derselben Adresse (Hostname und IP zählen getrennt). Anzeigen lassen sie sich, solange der Bereich im Verlauf liegt, sonst erscheinen sie blass.
- **Trigger wie am Oszilloskop**: hält an, sobald der Strom über eine Schwelle steigt (100-ms-Mittel oder 1-ms-Spitze, Flanke), mit wählbarem Vorlauf (10/25/50 % des Fensters). So lässt sich die nächste Wachphase abfangen, ohne davorzusitzen.
- **Wachphasen**: erfasste Wachphasen (`WAKE_CAPTURE=1`) erscheinen als Band im Verlauf und als Tabelle (Zeit, aktive Dauer, Ladung, Mittel, Spitze). Ein Klick auf eine Zeile springt zur Wachphase, solange sie im Verlauf liegt (älter als 2 Minuten in 100-ms-Werten). Eine laufende Wachphase ist markiert.
- **Kennzahlen des Loggers** wie in Home Assistant: Ruhestrom, geschätzte Laufzeit, Ladung seit Start, Wachphasen, Samplerate.
- **Export**: angezeigter Ausschnitt oder Auswahl als CSV (1-ms-Werte, bei langen Fenstern 100-ms-Werte) oder PNG.

Die y-Achse passt sich ruhig an (sofort größer, erst nach 3 s Ruhe wieder kleiner) und lässt sich mit „Achse halten“ festsetzen. Die Auflösung ist 1 ms. Einzelne Pulse in voller Auflösung (10 µs) stehen in den Wachphasen-Dateien.

![Live-Ansicht, Fenster 5 min, Bereich zwischen zwei Markern ausgemessen, Wachphasen als Bänder (simulierte Daten aus dem Demo-Modus)](docs/live-ansicht.png)

Einschalten: in `.env` `LIVE_PORT=8080` setzen, in `docker-compose.yml` den Abschnitt `ports` einkommentieren und den Container neu starten (der Prüfling verliert dabei kurz die Versorgung). Danach `http://<Pi>:8080/` öffnen.

Der Webserver nutzt nur die Python-Standardbibliothek (Server-Sent Events), bis zu 4 Browser gleichzeitig, ca. 25 kB/s je Browser, dazu beim Aufziehen langer Fenster einmalig ca. 0,7 MB je Stunde Verlauf. Am Raspberry Pi 4 kostet die Live-Ansicht etwa 2 % eines Kerns, die Samplerate bleibt bei 100 kSps. Fällt sie aus, läuft die Messung weiter. Es gibt keine Anmeldung, die Seite gehört nur ins Heimnetz.

Ohne PPK2 zeigt `python3 live_view.py --demo` die Seite mit simulierten Daten unter `http://127.0.0.1:8080/` (braucht numpy). Die Demo erzeugt vorab 48 h Verlauf mit Wachphasen, übernimmt ihn wie beim ersten Start in ein Projekt, und die Auswertung unter `/auswertung` ist nach ca. 50 s vollständig. Projekte lassen sich in der Demo anlegen und wechseln. Wer nur die Seiten `live_view.html`, `auswertung.html` oder `projekte.html` ändert, kann sie einzeln mit z. B. `docker cp auswertung.html ppk2-logger:/app/` in den laufenden Container kopieren, sie werden bei jedem Aufruf neu gelesen. Ein Neustart und damit ein Stromausfall am Prüfling ist dafür nicht nötig.

### Auswertung im Browser

Unter `http://<Pi>:8080/auswertung` (Reiter „Auswertung“ oben auf der Live-Seite) zeigt der Logger dasselbe wie `analyse.py` und `wake_view.py`, ohne Kommandozeile:

- Zeitraum 24 h, 48 h, 7 oder 30 Tage oder ab einem Zeitpunkt, dazu Schwelle für Wachphasen, Schwelle „teuer“, Akkukapazität und nutzbarer Anteil. Zeiten in der Zeitzone des Browsers.
- Kacheln mit Ruhestrom (Mittel, Median, P5–P95), Tagesverbrauch, Laufzeit und Wachphasen.
- Diagramme: Verlauf (Mittel und Max je Intervall, logarithmisch, Zoom durch Ziehen), Ruhestrom pro Stunde, Ladung pro Wachphase. Ein Klick auf eine Wachphase öffnet ihre Aufnahme.
- Wachphase in voller Auflösung (`WAKE_CAPTURE=1`): Mausrad zoomt bis auf einzelne Samples (10 µs), eine Auswahl wird exakt aus den Rohdaten ausgemessen (Dauer, Mittel, Spitze, Ladung). Export als CSV oder PNG.
- Tabellen pro Tag und pro Wachphase.

![Auswertung im Browser mit geöffneter Wachphase und ausgemessenem Bereich (simulierte Daten aus dem Demo-Modus)](docs/auswertung.png)

Gerechnet wird in eigenen Prozessen mit niedriger Priorität (`nice 10`): `analyse.py --json` pro Abruf (Ergebnis 60 s zwischengespeichert), `wake_view.py --serve` dauerhaft für die Wachphasen (hält die letzten 3 Aufnahmen im Speicher). So rechnen sie nicht im Logger-Prozess und bremsen den Lese-Thread nicht aus. Gelesen werden nur Dateien unter `data/`.

## Projekte

Jede Messung (Board, Prüfling, Aufbau) ist ein Projekt mit eigenem Ordner:

```
data/
  projekte.json               welches Projekt aktiv ist
  projekte/
    bisherige-messung/        projekt.json, ppk2_*.csv, wake/
    sensorknoten-nrf52/
```

Unter `http://<Pi>:8080/projekte` (Reiter „Projekte“) stehen das aktive Projekt mit den Live-Kennzahlen und eine Liste aller Projekte mit Zeitraum, Ruhestrom, Tagesverbrauch, Laufzeit und Speicher. Die Kennzahlen archivierter Projekte gelten für die ganze Messdauer. Sie werden einmal im Hintergrund berechnet und als `zusammenfassung.json` im Projekt abgelegt.

- **Neues Projekt**: Der Assistent fragt in vier Schritten Name und Board, Akku, Schwellen (Ruhestrom, Wachphasen-Aufnahme) ab und zeigt am Ende Modus und Spannung aus `.env` zur Kontrolle. Mit „Anlegen und starten“ wird das bisherige Projekt archiviert. Der Logger schreibt sofort in den neuen Ordner, ohne die Verbindung zum PPK2 zu schließen. Der Prüfling bleibt also versorgt, der Wechsel kostet keinen Stromschnitt. Ladungszähler und Laufzeitschätzung beginnen bei 0.
- **Fortsetzen**: Ein archiviertes Projekt wird wieder aktiv, neue Daten werden dort angehängt.
- **Bearbeiten**: Name, Board, Notiz und Einstellungen. Beim aktiven Projekt gelten sie sofort, die Zähler laufen weiter.
- **Auswerten**: öffnet `/auswertung` für das Projekt. Dort lässt sich das Projekt auch oben wählen, die Schwellen und der Akku kommen aus dem Projekt, für archivierte Projekte ist „ganze Messung“ voreingestellt.

Löschen geht nur von Hand: Ordner unter `data/projekte/` entfernen (nicht den des aktiven Projekts). Die Speichergrenze für Wachphasen gilt pro Projekt.

Beim ersten Start dieser Version verschiebt der Logger vorhandene `ppk2_*.csv` und `wake/` aus `data/` in das Projekt „Bisherige Messung“. Andere Dateien (z. B. Mitschnitte von `diag.py`) bleiben liegen. `analyse.py` und `wake_view.py` nehmen ohne `--dir` das aktive Projekt, für ein anderes `--dir data/projekte/<name>`.

Änderungen über die Seite lassen sich mit `ADMIN_TOKEN` in `.env` schützen. Der Browser fragt dann einmal nach dem Passwort und merkt es sich. Ohne Token darf jeder im Heimnetz Projekte anlegen und wechseln (gelesen werden kann ohnehin alles).

## Tests ohne Hardware

`tests/` enthält eine Simulation (synthetische Wachphasen, Fake-PPK2) für Recorder, Logger-Schleife, Live-Ansicht, Auswertung im Browser, Projekte (inkl. Wechsel während der Messung) und `wake_view.py`:

```bash
pip install numpy ppk2-api==0.9.2
python3 -m unittest discover tests
```

`tests/test_decoder.py` vergleicht den Dekoder (`ppk2_fast.py`) mit `PPK2_API.get_samples()` der Bibliothek, mit synthetischen Rohwörtern (inkl. Bereichswechseln, Spike-Filter und Fenstern über Blockgrenzen) und mit echten Rohbytes aus `tests/daten/` (Mitschnitt mit `diag.py --raw` am Pi 4, siehe unten). Auf den kompletten 60-s-Mitschnitten weicht er höchstens 2·10⁻¹⁰ µA von der Bibliothek ab.

## Diagnose

`diag.py` zeigt, was vom PPK2 wirklich ankommt, und kann die Rohbytes mitschneiden. Das PPK2 kann nur ein Programm gleichzeitig nutzen, der Logger muss dafür gestoppt sein. **Der Prüfling verliert dabei die Versorgung.**

```bash
docker compose stop ppk2-logger
docker compose run --rm ppk2-logger python diag.py --sekunden 60 --raw /data/roh_mp.bin
docker compose run --rm ppk2-logger python diag.py --sekunden 60 --raw /data/roh_direkt.bin --direkt
docker compose start ppk2-logger
```

Enthält das Image `diag.py` noch nicht (ältere Images oder ein Neubau steht aus), das Skript aus dem Checkout einbinden: `docker compose run --rm -v "$PWD/diag.py:/app/diag.py:ro" ppk2-logger python diag.py ...`

Ohne `--direkt` liest `diag.py` wie der Logger über `PPK2_MP` (Lese-Thread der Bibliothek), mit `--direkt` ohne Thread direkt vom Port. Die Auswertung nutzt den 6-Bit-Zähler in Bit 18–23 jedes Samples: Sprünge zeigen fehlende Samples, Sprünge auf den 40 000-Byte-Blockgrenzen von `PPK2_MP` verworfene Blöcke. `python3 diag.py --auswerten data/roh_mp.bin` wertet einen Mitschnitt später erneut aus, ohne PPK2.

Ein Stück Mitschnitt als Testdaten für den Dekoder (ca. 1 s, 400 kB):

```bash
head -c 400000 data/roh_mp.bin > tests/daten/pi4_ruhe.bin && cp data/roh_mp.json tests/daten/pi4_ruhe.json
```

## Bekannte Stolperfallen

- **Zwei Ports, nur einer zählt.** Der niedrigere ttyACM ist der Datenport, der höhere die Zephyr-Shell. Mit `PPK2_PORT` leer wählt der Logger den richtigen.
- **Im Ampere-Modus liefert VOUT nur Spannung, wenn die Ausgabe eingeschaltet ist.** Der Logger schaltet sie ein. Ohne das bleibt der Prüfling stromlos, obwohl VIN anliegt.
- **Ein Neustart des Containers trennt den Prüfling kurz von der Versorgung.** Wird die serielle Verbindung geschlossen, geht VOUT aus. Der Prüfling bootet danach neu. Das gilt auch bei einem Absturz.
- **Median und Maximum sind kein Ruhestrom.** Bei gepulsten Strömen (Ladeschaltungen, Wandler) liegt der Median nahe Null, obwohl der Mittelwert hoch ist. Aussagekräftig ist nur der Mittelwert über längere Fenster.
- **Docker-Rechte**: Der Container bekommt `/dev` und eine cgroup-Regel für ttyACM (Major 166), damit ein neu enumerierter Port weiter funktioniert.

## Beispielmessung

[ESP32-C6 mit E-Paper (Wetter und Kalender)](https://github.com/DocBigs-Lab/esp32c6-epaper-weather-calendar), 3000-mAh-Akku, von 5 bis 22 Uhr stündlich ein Aufwachen (18 pro Tag: WLAN, Wetterabruf, Display-Refresh). Jede Zeile der Tabelle baut auf der vorigen auf:

| Stand | Ruhestrom (Deep Sleep) | Wachphase mit Refresh | Tagesverbrauch | Laufzeit |
|---|---|---|---|---|
| Auslieferungszustand (laut Hardware-Doku, nicht mit diesem Logger gemessen) | ca. 3,2 mA | – | – | ca. 5–6 Wochen |
| PWR-LED und R12 (Pull-up an BS) ausgelötet | ca. 1390 µA | ca. 310 µAh, 30 s | ca. 37 mAh | ca. 65 Tage |
| zusätzlich R2 (Pull-up an BUSY) ausgelötet | ca. 156 µA | ca. 310 µAh, 30 s | ca. 9,5 mAh | ca. 250 Tage |
| Firmware: Light Sleep während des Refreshs (24 h 10./11.11.) | 149 µA (P5 94, P95 213) | Mittel 199 µAh, 27–33 s | 7,1 mAh, 50 % Wachphasen | ca. 337 Tage |
| Firmware: feste Pegel an DC und LED im Deep Sleep (24 h 13./14.11.) | **71 µA** (P5 41, P95 111) | Mittel 214 µAh, 26–39 s | 5,6 mAh, 69 % Wachphasen | **ca. 432 Tage** |

Zwei Umbauten an der Hardware und zwei an der Firmware. Die Einzelheiten zu den Widerständen stehen in der [Hardware-Doku des Displays](https://github.com/DocBigs-Lab/esp32c6-epaper-weather-calendar/blob/main/docs/hardware.md). Die Laufzeit rechnet mit 80 % der Nennkapazität und ohne Selbstentladung des Akkus.

Die 24-h-Messung (09./10.11.) zeigt 18 Wachphasen zwischen 170 und 225 µAh, dazu zwei Ausreißer mit 262 und 398 µAh bei 38 s. Die Wachphasen-Aufnahme ergibt für dieselben 18 Wachphasen 3760 µAh, `analyse.py` 3770 µAh. Eine Wachphase ohne Display-Refresh kostet ca. 100 µAh in 8 s. Der Rückgang von ca. 310 auf ca. 190 µAh pro Wachphase kommt vom Light Sleep während des Refreshs (Firmware vom 09.11.): Der ESP schläft, während das Panel ca. 22 s arbeitet, statt wach auf BUSY zu warten.

In der zweiten 24-h-Messung (10./11.11.) hat das Display in allen 18 Wachphasen neu gezeichnet (163–273 µAh). Das ist kein Fehler: Die Anzeige enthält die aktuelle Temperatur, und die ändert sich fast jede Stunde. Wachphasen ohne Neuzeichnen (ca. 100 µAh) kämen auf ca. 450 Tage, das wurde bewusst nicht weiter verfolgt.

In der Live-Ansicht zeigt sich, woraus der Ruhestrom besteht: Die Grundlinie liegt bei etwa 1–10 µA, dazu kommt etwa alle 5 ms ein Puls, der im 1-ms-Mittel 10–60 mA erreicht, vermutlich vom Spannungswandler im Burst-Betrieb. Die rund 150 µA Ruhestrom entstehen fast ganz aus diesen Pulsen.

**Schwebende Pins im Deep Sleep (13.11.).** Der Ruhestrom schwankte stundenweise zwischen ca. 120 und 200 µA, in einer Nacht lag er fünf Stunden lang bei ca. 190 µA. Die Verteilung der Sekundenwerte verschob sich dabei als Ganzes. Kurze Zwischen-Wachphasen kamen also nicht in Frage, sondern eher ein Eingang des ESP ohne festen Pegel. In der Firmware des Displays lagen zwei Pins im Deep Sleep hochohmig, ohne dass etwas sie treibt (DC zum Display-Controller und der LED-Pin). Seit sie im Schlaf fest auf HIGH gehalten werden, liegt der Ruhestrom bei ca. 70 µA statt ca. 150 µA:

![Auswertung über 4 h: bis 12:23 Uhr Ruhestrom ca. 150 µA, nach dem Flashen der geänderten Firmware nach ca. 30 min ca. 70 µA](docs/firmware-pins-vorher-nachher.png)

Die Kacheln mischen hier beide Zustände, und die vier Stunden enthalten den Kaltstart nach dem Flashen. Laufzeit und Tagesverbrauch sind deshalb nicht aussagekräftig. Die 24-h-Messung danach (letzte Spalte der Tabelle) zeigt 71 µA, stündlich 67–76 µA, auch nachts ohne Erhöhung. Damit machen die Wachphasen gut zwei Drittel des Verbrauchs aus.

## Grenzen

- Die Statistik pro Intervall zeigt keine einzelnen Stromspitzen. Dafür gibt es die Live-Ansicht (1 ms), die Wachphasen-Aufnahme (`WAKE_CAPTURE`, 10 µs) oder die Nordic-App.
- Bis Version 11/2026 kamen am Raspberry Pi 4 nur ca. 88.000 statt 100.000 Samples pro Sekunde an. Ursache (geklärt mit `diag.py`): Bei gepulstem Strom wechselt das PPK2 ca. 11 000-mal pro Sekunde den Messbereich, fast jeder Block lief durch die langsame Python-Schleife der Bibliothek, der Lese-Thread kam zu selten dran und das PPK2 sendete weniger. Seit der Spike-Filter im Dekoder vektorisiert ist, entfällt diese Last: am selben Pi kommen jetzt im Mittel 100 333 Samples pro 1-s-Zeile an. Mittelwert und Ladung waren auch vorher richtig (Mittel × Zeit).
- Der Container belegt am Pi 4 etwa einen Kern, auch ohne Browser. Das kommt vom Lese-Thread der Bibliothek, der den Port in einer engen Schleife abfragt. Für die Messung ist das kein Problem, der Pi hat vier Kerne.
- Die Laufzeit ist eine Hochrechnung aus dem Durchschnitt seit Projektstart und braucht mindestens einen vollständigen Zyklus des Prüflings. Eine Anlaufphase (Kaltstart, Flashen) drückt sie noch lange nach unten. Für einen Bereich ohne Anlaufphase zeigt die Live-Ansicht „Akku ≙ N Tage“ in der Auswahl, z. B. ab einem Marker bis jetzt.
- Getestet mit PPK2, `ppk2-api-python` 0.9.2 auf einem Raspberry Pi. Die Bibliotheksversion ist im Dockerfile festgenagelt, weil `ppk2_fast.py` interne Zustände der Bibliothek mitführt.

## Siehe auch

- [esp32c6-epaper-weather-calendar](https://github.com/DocBigs-Lab/esp32c6-epaper-weather-calendar): das Wetter- und Kalender-Display aus der [Beispielmessung](#beispielmessung). Mit diesem Logger gemessen und dabei optimiert: Pull-ups ausgelötet, Light Sleep während des Refreshs, feste Pegel im Deep Sleep. Die Hardware-Doku dort beschreibt die Umbauten.
- [ppk2.html](https://github.com/pilate/ppk2.html) von pilate: eine einzelne HTML-Seite, die das PPK2 per Web Serial direkt im Browser ausliest, ohne Installation. Sie zeigt die vollen 100 kHz statt der 1-ms-Werte der Live-Ansicht hier und taugt damit für den Blick auf einzelne Pulse. Für Messungen über Tage ist sie nicht gedacht, weil sie die Aufnahme im Speicher hält. Beide können nicht gleichzeitig auf das PPK2 zugreifen: Vor dem Einsatz der Seite den Container stoppen (`docker compose stop`) und danach wieder starten. Der Prüfling verliert dabei kurz die Versorgung.

## Lizenz

GPL-2.0-only, siehe [LICENSE](LICENSE). Das Projekt nutzt und erweitert [`ppk2-api-python`](https://github.com/IRNAS/ppk2-api-python) von IRNAS (GPL-2.0), `ppk2_fast.py` bildet deren Dekodierung nach. Das PPK2 ist ein Produkt von Nordic Semiconductor, dieses Projekt steht in keiner Verbindung zu Nordic.

Ein Projekt von [DocBigsLab](https://docbigs-lab.de) – entwickelt, nicht gebastelt.
