"""Lay the exported pod STLs print-side down, ready for any slicer.

    python hardware/enclosure/stl_orient.py

Reads stl/pod_case_raw.stl + stl/pod_lid_raw.stl (from the Fusion script pod_export),
writes stl/pod_case.stl (floor on the bed, as modeled) and stl/pod_lid.stl (flipped so
the flat top is on the bed). Also checks size, units and that each mesh is closed.
"""
from collections import Counter
from pathlib import Path

import numpy as np

STL = Path(__file__).parent / "stl"
DT = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])


def read(p):
    b = p.read_bytes()
    n = int(np.frombuffer(b, "<u4", 1, 80)[0])
    return np.frombuffer(b, DT, n, 84)["v"].astype(float)


def write(p, tris):
    a = tris[:, 1] - tris[:, 0]
    c = tris[:, 2] - tris[:, 0]
    nrm = np.cross(a, c)
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-12
    out = np.zeros(len(tris), DT)
    out["n"], out["v"] = nrm, tris
    p.write_bytes(b"Bar Tracker pod".ljust(80, b" ") + np.uint32(len(tris)).tobytes() + out.tobytes())


def closed(tris):
    # every edge of a closed mesh is shared by exactly two triangles
    keys = np.round(tris, 4)
    edges = Counter()
    for t in keys:
        for i in range(3):
            a, b = tuple(t[i]), tuple(t[(i + 1) % 3])
            edges[(a, b) if a < b else (b, a)] += 1
    return sum(1 for k in edges.values() if k != 2)


for name, flip in [("case", False), ("lid", True)]:
    tris = read(STL / f"pod_{name}_raw.stl")
    size = np.ptp(tris.reshape(-1, 3), axis=0)
    if size.max() < 10:          # Fusion wrote cm: make it mm (slicers assume mm)
        tris *= 10
    if flip:                     # turn over about x: top face goes onto the bed
        tris[..., 1] *= -1
        tris[..., 2] *= -1
    pts = tris.reshape(-1, 3)
    tris -= [pts[:, 0].min(), pts[:, 1].min(), pts[:, 2].min()]
    write(STL / f"pod_{name}.stl", tris)
    size = np.ptp(tris.reshape(-1, 3), axis=0)
    print(f"pod_{name}.stl: {size[0]:.2f} x {size[1]:.2f} x {size[2]:.2f} mm, {len(tris)} triangles, "
          f"open edges {closed(tris)}")
