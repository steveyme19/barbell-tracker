"""
Load-velocity profile and e1RM (roadmap 4.3). Reference for webapp/lv.js.

One point per set: (load, best-rep MCV). The fastest rep is the least fatigued, which
is how load-velocity profiles are usually built. Straight line v = a + b * load by least
squares; needs 2+ different loads. e1RM = the load where the line reaches the lift's
mean velocity at 1RM (analytics.md section 5, [1]): squat 0.23, bench 0.10, deadlift
0.14 m/s. Machines and unknown free weights have no published 1RM velocity: profile
only (the usual speed at a load), no e1RM. An e1RM more than E1RM_MAX_X times the
heaviest load used is too far to extrapolate and isn't reported.

Usage:
    python lv.py squat "[[135, 0.82], [185, 0.66], [225, 0.51]]"
    python lv.py squat "[[135, 0.82], [185, 0.66]]" --json      # for the JS parity test
"""

import json
import sys

from sets import V1RM          # same 1RM velocities the RPE estimate uses

E1RM_MAX_X = 2.0               # don't report an e1RM past 2x the heaviest load used


def fit_profile(points, lift):
    """points: [(load, mcv), ...]. Returns dict(a, b, r2, n, loads, max_load, e1rm) or None."""
    pts = [(float(l), float(v)) for l, v in points if l is not None and l > 0 and v > 0]
    loads = sorted({l for l, _ in pts})
    if len(loads) < 2:
        return None
    n = len(pts)
    mx = sum(l for l, _ in pts) / n
    my = sum(v for _, v in pts) / n
    sxx = sum((l - mx) ** 2 for l, _ in pts)
    sxy = sum((l - mx) * (v - my) for l, v in pts)
    syy = sum((v - my) ** 2 for _, v in pts)
    b = sxy / sxx
    a = my - b * mx
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 1.0
    e1rm = None
    if lift in V1RM and b < 0:
        e = (V1RM[lift] - a) / b
        if 0 < e <= E1RM_MAX_X * loads[-1]:
            e1rm = e
    return dict(a=a, b=b, r2=r2, n=n, loads=len(loads), max_load=loads[-1], e1rm=e1rm)


def main():
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(args) < 2:
        sys.exit(__doc__)
    p = fit_profile(json.loads(args[1]), args[0])
    if "--json" in sys.argv:
        print(json.dumps(p))
        return
    if p is None:
        print("Need sets at 2+ different loads.")
        return
    print(f"v = {p['a']:.3f} {p['b']:+.5f} x load  (R^2 {p['r2']:.2f}, {p['n']} sets, {p['loads']} loads)")
    print(f"e1RM: {p['e1rm']:.0f}" if p["e1rm"] else "e1RM: n/a for this lift or too far to extrapolate")


if __name__ == "__main__":
    main()
