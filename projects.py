# SPDX-License-Identifier: GPL-2.0-only
"""Projekte: jede Messung (Board, Prüfling) bekommt einen eigenen Ordner.

Ablage unter OUT_DIR:
    projekte.json                 {"version": 1, "aktiv": "<slug>"}
    projekte/<slug>/projekt.json  Name, Board, Notiz, Zeiten, Einstellungen
    projekte/<slug>/ppk2_*.csv    Tagesdateien wie bisher
    projekte/<slug>/wake/         Wachphasen wie bisher

Beim ersten Start mit dieser Version werden vorhandene Tagesdateien und der Ordner wake/
aus OUT_DIR in ein Projekt "Bisherige Messung" verschoben (gleiches Dateisystem, nur
umbenannt). Andere Dateien (z. B. Mitschnitte von diag.py) bleiben liegen.

Der Logger fragt pro Schleifendurchlauf nur store.gen ab (eine Zahl). Ändert sie sich,
holt er sich mit snapshot() Ordner und Einstellungen und stellt um, ohne die serielle
Verbindung zu schließen. Modus und Spannung (PPK2_MODE, PPK2_VOLTAGE_MV) gehören nicht
dazu: die bleiben in .env, ein Wechsel soll bewusst und von Hand passieren.

Nur Standardbibliothek (wird auch von analyse.py benutzt).
"""
import glob
import json
import os
import re
import threading
from datetime import datetime, timezone

STATE = "projekte.json"
SUBDIR = "projekte"
META = "projekt.json"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

# Einstellungen pro Projekt: Name -> (Typ, min, max). Vorbelegung aus .env.
SETTINGS = {
    "battery_mAh": (float, 1.0, 1e6),
    "battery_usable": (float, 0.01, 1.0),
    "quiet_max_uA": (float, 1.0, 1e7),
    "redraw_uAh": (float, 0.0, 1e7),
    "wake_capture": (bool, None, None),
    "wake_trigger_uA": (float, 1.0, 1e7),
    "wake_trigger_ms": (float, 0.01, 10_000.0),
    "wake_pre_s": (float, 0.0, 60.0),
    "wake_post_s": (float, 0.01, 60.0),
    "wake_max_s": (float, 1.0, 3600.0),
    "wake_max_mb": (float, 10.0, 1e7),
}
TEXT_MAX = {"name": 60, "board": 80, "notiz": 1000}


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name):
    s = name.strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40].strip("-")
    return s or "projekt"


def check_settings(d, base=None):
    """Einstellungen prüfen und vervollständigen. Unbekannte Schlüssel sind ein Fehler."""
    out = dict(base or {})
    for k, v in (d or {}).items():
        if k not in SETTINGS:
            raise ValueError(f"unbekannte Einstellung {k}")
        typ, lo, hi = SETTINGS[k]
        if typ is bool:
            if isinstance(v, str):
                v = v.strip().lower() in ("1", "true", "yes", "on", "ja")
            out[k] = bool(v)
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{k}: keine Zahl")
        if not (lo <= v <= hi):          # fängt auch NaN ab
            raise ValueError(f"{k}: erlaubt {lo:g} bis {hi:g}")
        out[k] = v
    missing = [k for k in SETTINGS if k not in out]
    if missing:
        raise ValueError("fehlende Einstellungen: " + ", ".join(missing))
    return out


def _text(d, k, required=False):
    v = d.get(k)
    if v is None:
        if required:
            raise ValueError(f"{k} fehlt")
        return None
    v = str(v).strip()
    if required and not v:
        raise ValueError(f"{k} fehlt")
    if len(v) > TEXT_MAX[k] or any(ord(c) < 32 and c not in "\n\t" for c in v):
        raise ValueError(f"{k}: zu lang oder ungültige Zeichen")
    return v


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _first_ts(path):
    try:
        with open(path, newline="") as f:
            f.readline()
            line = f.readline()
        return line.split(",", 1)[0] or None
    except OSError:
        return None


def _last_ts(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4096))
            lines = f.read().decode("utf-8", "replace").strip().splitlines()
        if len(lines) < 1 or lines[-1].startswith("timestamp_utc"):
            return None
        return lines[-1].split(",", 1)[0] or None
    except OSError:
        return None


def dir_size(d):
    total = 0
    for root, _, names in os.walk(d):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return total


def resolve_dir(d):
    """Für Werkzeuge (analyse.py, wake_view.py): zeigt d auf OUT_DIR mit Projekten,
    wird der Ordner des aktiven Projekts benutzt."""
    try:
        with open(os.path.join(d, STATE), encoding="utf-8") as f:
            slug = json.load(f).get("aktiv", "")
    except (OSError, ValueError):
        return d
    p = os.path.join(d, SUBDIR, slug)
    return p if SLUG_RE.match(slug or "") and os.path.isdir(p) else d


class ProjectStore:
    def __init__(self, base, defaults, fixed=None, log=None):
        """base: OUT_DIR. defaults: Einstellungen aus .env (vollständig).
        fixed: Werte, die nur angezeigt werden (Modus, Spannung)."""
        self.base = base
        self.root = os.path.join(base, SUBDIR)
        self.defaults = check_settings(defaults)
        self.fixed = dict(fixed or {})
        self.log = log or (lambda s: print(s, flush=True))
        self.gen = 0                      # zählt jede Änderung am aktiven Projekt
        self._lock = threading.RLock()
        self._meta = {}
        self._active = None
        self._load()

    # ------------------------------------------------------------ Laden, Migration
    def _load(self):
        os.makedirs(self.root, exist_ok=True)
        state_path = os.path.join(self.base, STATE)
        state = {}
        if os.path.exists(state_path):
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
        for p in sorted(glob.glob(os.path.join(self.root, "*", META))):
            slug = os.path.basename(os.path.dirname(p))
            if not SLUG_RE.match(slug):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    m = json.load(f)
                m["settings"] = check_settings(m.get("settings", {}), self.defaults)
            except (OSError, ValueError) as e:
                self.log(f"Projekt {slug} übersprungen: {e!r}")
                continue
            self._meta[slug] = m
        active = state.get("aktiv")
        if active not in self._meta:
            active = self._migrate()
        self._active = active
        self._save_state()

    def _migrate(self):
        """Ohne gültiges aktives Projekt: bisherige Daten übernehmen oder leeres anlegen."""
        legacy = sorted(glob.glob(os.path.join(self.base, "ppk2_*.csv*")))
        wake = os.path.join(self.base, "wake")
        if legacy or os.path.isdir(wake):
            first = _first_ts(legacy[0]) if legacy else None
            slug = self._new(name="Bisherige Messung", board="", notiz="Automatisch übernommen "
                             "aus dem Datenordner (Daten vor der Projektverwaltung).",
                             settings=self.defaults, created=first)
            d = self.dir_of(slug)
            for p in legacy:
                os.replace(p, os.path.join(d, os.path.basename(p)))
            if os.path.isdir(wake):
                os.replace(wake, os.path.join(d, "wake"))
            self.log(f"Bisherige Daten nach {SUBDIR}/{slug}/ verschoben "
                     f"({len(legacy)} CSV-Dateien{', Wachphasen' if os.path.isdir(os.path.join(d, 'wake')) else ''})")
            return slug
        if self._meta:                     # Zustand verloren, Projekte da: neuestes nehmen
            return max(self._meta, key=lambda s: self._meta[s].get("angelegt") or "")
        return self._new(name="Projekt 1", board="", notiz="", settings=self.defaults)

    def _new(self, name, board, notiz, settings, created=None):
        slug, i = slugify(name), 2
        base_slug = slug[:36]
        while slug in self._meta or os.path.exists(os.path.join(self.root, slug)):
            slug, i = f"{base_slug}-{i}", i + 1
        os.makedirs(os.path.join(self.root, slug))
        m = {"name": name, "board": board or "", "notiz": notiz or "",
             "angelegt": created or now_utc(), "archiviert": None,
             "fest": dict(self.fixed), "settings": dict(settings)}
        self._meta[slug] = m
        self._save_meta(slug)
        return slug

    def _save_meta(self, slug):
        _write_json(os.path.join(self.root, slug, META), self._meta[slug])

    def _save_state(self):
        _write_json(os.path.join(self.base, STATE), {"version": 1, "aktiv": self._active})

    # ------------------------------------------------------------ Lesen
    @property
    def active(self):
        return self._active

    def dir_of(self, slug):
        if not SLUG_RE.match(slug or "") or slug not in self._meta:
            raise ValueError("unbekanntes Projekt")
        return os.path.join(self.root, slug)

    def snapshot(self):
        """(gen, {slug, name, dir, settings}) des aktiven Projekts, für den Logger."""
        with self._lock:
            m = self._meta[self._active]
            return self.gen, {"slug": self._active, "name": m["name"],
                              "dir": self.dir_of(self._active),
                              "settings": dict(m["settings"])}

    def get(self, slug):
        with self._lock:
            self.dir_of(slug)
            return json.loads(json.dumps(self._meta[slug]))

    def info(self, slug):
        """Metadaten plus Angaben aus den Dateien (Zeitraum, Größe)."""
        m = self.get(slug)
        d = self.dir_of(slug)
        files = sorted(glob.glob(os.path.join(d, "ppk2_*.csv")))
        m.update(slug=slug, aktiv=(slug == self._active), csv_dateien=len(files),
                 von=_first_ts(files[0]) if files else None,
                 bis=_last_ts(files[-1]) if files else None,
                 mb=round(dir_size(d) / 1e6, 1),
                 wake_mb=round(dir_size(os.path.join(d, "wake")) / 1e6, 1))
        return m

    def list(self):
        with self._lock:
            slugs = list(self._meta)
        out = [self.info(s) for s in slugs]
        out.sort(key=lambda m: (not m["aktiv"], -(_ts_key(m.get("bis") or m["angelegt"]))))
        return out

    # ------------------------------------------------------------ Ändern
    def create(self, name, board="", notiz="", settings=None, activate=True):
        d = {"name": name, "board": board, "notiz": notiz}
        name = _text(d, "name", required=True)
        board, notiz = _text(d, "board") or "", _text(d, "notiz") or ""
        s = check_settings(settings or {}, self.defaults)
        with self._lock:
            slug = self._new(name, board, notiz, s)
            if activate:
                self._switch(slug)
        self.log(f"Projekt angelegt: {name} ({slug})")
        return slug

    def activate(self, slug):
        with self._lock:
            self.dir_of(slug)
            if slug != self._active:
                self._switch(slug)
        return slug

    def _switch(self, slug):
        old = self._active
        if old in self._meta:
            self._meta[old]["archiviert"] = now_utc()
            self._save_meta(old)
        self._meta[slug]["archiviert"] = None
        self._save_meta(slug)
        self._active = slug
        self._save_state()
        self.gen += 1
        self.log(f"Aktives Projekt: {self._meta[slug]['name']} ({slug}), "
                 f"archiviert: {old}")

    def update(self, slug, name=None, board=None, notiz=None, settings=None):
        d = {"name": name, "board": board, "notiz": notiz}
        with self._lock:
            self.dir_of(slug)
            m = self._meta[slug]
            new = dict(m)
            if name is not None:
                new["name"] = _text(d, "name", required=True)
            if board is not None:
                new["board"] = _text(d, "board")
            if notiz is not None:
                new["notiz"] = _text(d, "notiz")
            if settings is not None:
                new["settings"] = check_settings(settings, m["settings"])
            self._meta[slug] = new
            self._save_meta(slug)
            if slug == self._active:
                self.gen += 1
        return slug


def _ts_key(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return 0.0


class FixedDir:
    """Ersatz für ProjectStore ohne Projektverwaltung (Tests, alte Aufrufe):
    fester Ordner, feste Einstellungen."""

    gen = 0

    def __init__(self, d, settings):
        self._snap = {"slug": "", "name": "", "dir": d, "settings": dict(settings)}

    def snapshot(self):
        return 0, dict(self._snap, settings=dict(self._snap["settings"]))


__all__ = ["ProjectStore", "FixedDir", "SETTINGS", "check_settings", "resolve_dir", "slugify"]
