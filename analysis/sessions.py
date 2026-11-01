"""
Read a Bar Tracker session export (the app's Export / Export all: webapp/export.js, D30) and,
if asked, write every set back out as the app's set CSV, so every script here works on it.

    python sessions.py data/session-2026-....json             # list the sessions and their sets
    python sessions.py data/....json --csv=data/session-X/     # each set as its bar-<lift>-...csv

load(path) -> [{"record": {...}, "sets": [{"index", "n", "t", "ax", ..., "gz", "tAbs"}]}], the
columns as numpy arrays with the exact values the app stored (t and tAbs float64, the sensor
readings float32: the app's live CSVs were written from the readings before that, so a CSV from
here can differ from the live one in the 7th significant digit).

The app stores what it corrected each part's samples with (the record's "calibration": the pod's
pod-calibration.json, D42, or the phone's 6-face one, D23) and each set's part. --csv writes that
next to the sets as <session id>-calibration.json (calibration_json), so nothing corrects them twice
(reps.py --calib refuses).
"""

import base64
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

FORMAT, VERSION = "bartracker-sessions", 1
COLS = ["t", "ax", "ay", "az", "gx", "gy", "gz"]
DTYPES = {"f64": "<f8", "f32": "<f4"}            # typed arrays' bytes, little-endian
HEADER = ('"Time (s)","Acceleration x (m/s^2)","Acceleration y (m/s^2)","Acceleration z (m/s^2)",'
          '"Absolute acceleration (m/s^2)","Gyro x (deg/s)","Gyro y (deg/s)","Gyro z (deg/s)"')


def column(c):
    return np.frombuffer(base64.b64decode(c["b64"]), dtype=DTYPES[c["type"]])


def exported_at(path):
    """When the export was made (its ISO "exported" time, "" if it has none), from the file's first
    bytes: the app writes format, version and exported before the sessions (export.js), so a file of
    hundreds of MB isn't parsed for it."""
    with open(path, "rb") as f:
        m = re.search(rb'"exported"\s*:\s*"([^"]*)"', f.read(4096))
    return m.group(1).decode() if m else ""


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


def csv_name(record, index):
    """A stored set's CSV name: the record's, or one like the app's for a set the record hasn't got."""
    meta = next((x for x in record["sets"] if x["index"] == index), {})
    return meta.get("file") or f"bar-{meta.get('lift') or 'free'}-{record['stamp']}-set{index + 1:02d}.csv"


def calibration_json(s):
    """<session id>-calibration.json for a loaded session: what the app corrected each part's samples with
    (the record's "calibration": {part: {"source": "pod" | "phone", "serial", "calibration": what was
    applied, or None for raw}}) and each set CSV's part (None: a set stored before the app said it).
    None for a record from before D42 (no "calibration")."""
    r = s["record"]
    if not r.get("calibration"):
        return None
    part = {x["index"]: x.get("part") for x in r["sets"]}
    return {"what": "what the app corrected this session's samples with, per part (D42): a set CSV whose part's "
                    "calibration isn't null is corrected already",
            "session": r["id"], "parts": r["calibration"],
            "sets": {csv_name(r, st["index"]): part.get(st["index"]) for st in s["sets"]}}


def part_text(p):
    """One part's calibration, for the listing: "pod 0x41ef151f, corrected (calibration made 2026-10-31)"."""
    p = p or {}
    who = p.get("source", "?")
    if who == "pod" and isinstance(p.get("serial"), int):
        who += f" 0x{p['serial']:08x}"                # as podlink.py prints it
    c = p.get("calibration")
    if not c:
        return f"{who}, raw (no calibration applied)"
    # the pod's says when it was made (ISO); the phone's has the date it was taken (ms)
    made = c.get("made") or (datetime.fromtimestamp(c["date"] / 1000).isoformat() if isinstance(c.get("date"), (int, float)) else "?")
    return f"{who}, corrected (calibration made {made[:10]})"


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
        for k, p in sorted((r.get("calibration") or {}).items(), key=lambda kv: int(kv[0])):
            print(f"  part {k}: {part_text(p)}")
        for st in s["sets"]:
            meta = by_index.get(st["index"], {})
            name = csv_name(r, st["index"])
            print(f"  set {st['index'] + 1}: {meta.get('exercise') or meta.get('lift') or '?'}, {meta.get('nReps')} reps, "
                  f"{st['n']} samples, {name}")
            if out_dir is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / name).write_text(set_csv(st))
        cj = calibration_json(s)
        if out_dir is not None and cj:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{r['id']}-calibration.json").write_text(json.dumps(cj, indent=1), encoding="utf-8")
    if out_dir is not None:
        print(f"wrote the sets to {out_dir}, with each session's calibration (<session id>-calibration.json)")


if __name__ == "__main__":
    main()
