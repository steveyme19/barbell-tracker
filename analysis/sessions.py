"""
Read a Bar Tracker session export (the app's Export / Export all: webapp/export.js, D30) and,
if asked, write every set back out as the app's set CSV, so every script here works on it.

    python sessions.py data/session-2026-....json             # list the sessions and their sets
    python sessions.py data/....json --csv=data/session-X/     # each set as its bar-<lift>-...csv

load(path) -> [{"record": {...}, "sets": [{"index", "n", "t", "ax", ..., "gz", "tAbs"}]}], the
columns as numpy arrays with the exact values the app stored (t and tAbs float64, the sensor
readings float32: the app's live CSVs were written from the readings before that, so a CSV from
here can differ from the live one in the 7th significant digit).
"""

import base64
import json
import sys
from pathlib import Path

import numpy as np

FORMAT, VERSION = "bartracker-sessions", 1
COLS = ["t", "ax", "ay", "az", "gx", "gy", "gz"]
DTYPES = {"f64": "<f8", "f32": "<f4"}            # typed arrays' bytes, little-endian
HEADER = ('"Time (s)","Acceleration x (m/s^2)","Acceleration y (m/s^2)","Acceleration z (m/s^2)",'
          '"Absolute acceleration (m/s^2)","Gyro x (deg/s)","Gyro y (deg/s)","Gyro z (deg/s)"')


def column(c):
    return np.frombuffer(base64.b64decode(c["b64"]), dtype=DTYPES[c["type"]])


def load(path):
    obj = json.loads(Path(path).read_text(encoding="utf-8"))   # the app writes UTF-8 (typed names)
    if obj.get("format") != FORMAT:
        raise ValueError(f"{path}: not a Bar Tracker session export")
    if obj.get("version") != VERSION:
        raise ValueError(f"{path}: export version {obj.get('version')}, this reads {VERSION}")
    out = []
    for s in obj["sessions"]:
        sets = []
        for e in s["sets"]:
            d = {"index": e["index"], "n": e["n"], "tAbs": column(e["tAbs"])}
            for c in COLS:
                d[c] = column(e["cols"][c])
                if len(d[c]) != e["n"]:
                    raise ValueError(f"{s['record']['id']} set {e['index']}: {c} has {len(d[c])} samples, not {e['n']}")
            sets.append(d)
        out.append({"record": s["record"], "sets": sets})
    return out


def set_csv(st):
    """One set as the app's CSV (fileio.js toCSV): t from the set's first sample, 7 significant digits."""
    t = st["t"] - st["t"][0] if st["n"] else st["t"]
    a = np.sqrt(st["ax"].astype(float) ** 2 + st["ay"].astype(float) ** 2 + st["az"].astype(float) ** 2)
    cols = [t, st["ax"], st["ay"], st["az"], a, st["gx"], st["gy"], st["gz"]]
    rows = (",".join(f"{float(c[i]):.7g}" for c in cols) for i in range(st["n"]))
    return HEADER + "\n" + "\n".join(rows) + "\n"


def main():
    sys.stdout.reconfigure(errors="replace")      # a cp1252 console can't print every typed exercise name
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    out_dir = next((Path(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--csv=")), None)
    for s in load(args[0]):
        r = s["record"]
        by_index = {x["index"]: x for x in r["sets"]}
        print(f"{r['id']}: {len(s['sets'])} sets stored, {len(r['sets'])} in the record"
              f"{'' if r.get('ended') else ' (unfinished)'}")
        for st in s["sets"]:
            meta = by_index.get(st["index"], {})
            name = meta.get("file") or f"bar-{meta.get('lift') or 'free'}-{r['stamp']}-set{st['index'] + 1:02d}.csv"
            print(f"  set {st['index'] + 1}: {meta.get('exercise') or meta.get('lift') or '?'}, {meta.get('nReps')} reps, "
                  f"{st['n']} samples, {name}")
            if out_dir is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / name).write_text(set_csv(st))
    if out_dir is not None:
        print(f"wrote the sets to {out_dir}")


if __name__ == "__main__":
    main()
