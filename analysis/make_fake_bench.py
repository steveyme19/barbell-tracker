"""
Fake bench sets with an exact known answer (D43): one generator, named scenarios.

    python make_fake_bench.py                        # the guard set parity.mjs checks (GUARDS, raw and cal)
    python make_fake_bench.py --list                 # every scenario, the issues it pins, what it is
    python make_fake_bench.py [names] [--seeds=1,2,3] [--sensor=raw|cal|both]
                                                     # data/fake_bench_<name>_s<seed>_<sensor>.csv + _truth.json

The 11-01 bench synthetics (D40 Open) found seven ways paused and racked bench went wrong; the D43
design workflow built this suite to pin them (30 scenarios, plus a stress one) and scored every fix on it.
The names in the comments are that workflow's generators it consolidates: synth-paused (paused and heavy
sets, shaped concentrics, shake on the carries, which keeps a smooth J-hook carry from passing the still
test), synth-tng (touch-and-go light and working sets, the fixed-arrival chest bounce, no-pause top
turns, sticking knots, the hard bounce), the code-reader (the soft rerack, 4 and 6 cm unracks) and
synth-edge (the 5-rep base, the rerack fumble, the pod pickup). One rep loop builds all of them, so every
scenario shares one mount, one sensor and one rerack model.

Physics: make_fake_motion.py's gym machinery (imported): quintic knots
(t, z, v, a) for the vertical path, tracks with exact derivatives for everything added on top, the pod on
Adapter A (R_POD: sensor x up at roll 0, y forward, z along the bar = BAR_POD), its raw accel offset,
scale, noise and LSB, its gyro bias, noise and LSB, at 208 Hz. World: x toward the lifter's feet, y along
the bar, z up; heights above the bar resting in the J-hooks. "cal" is the same draw with the app's
calibration taken out, (raw - offset) / scale (D42); "raw" is what an uncalibrated pod writes.

The truth (in each _truth.json): every rep's t_start, t_end, dur, rom and mcv from the exact knot path,
the app's definition of a rep. Touch-and-go: from the bottom of the chest bounce (v = 0) to the lockout
(v = 0). Paused: from the end of the chest pause (the press starts from v = 0 on the sunk chest) to the
lockout. rom = z_top - z_bottom, mcv = rom / dur. The tremble, breathing, chest vibration, effort
shake, clangs and handling ride on top of the path (all under 2 mm) and are not in the truth. Every
concentric is checked to rise all the way and every descent to fall all the way.

Time line of every set: racked still (3 s), the unrack (press `unrack` m out of the hooks in 0.5 s, carry
25 cm toward the feet in 0.8 s, a 5 mm settle), a hold at lockout, the reps, the last hold, the carry
back, the rerack (hard: lowered into the hooks at 0.30 m/s, a knock on the uprights and three clangs, the
sleeve spinning on; soft: set in at 0.06 m/s, no clang; fumble: the bar hits the upright face, bounces
out, is lifted 3 cm above lockout to re-aim, then lowered in), then 17 s racked so the app's 15 s gap
closes the set (the double buzz) inside the file. A pod pickup adds its own 17 s.
"""

import json
import sys
from pathlib import Path

import numpy as np

import make_fake_motion as M

DATA = Path(__file__).resolve().parent / "data"
FS, G = M.FS, M.G
LOCK = 0.05                           # m: lockout above the hooks = the unrack's lift (default)
CARRY = 0.25                          # m: hooks to over the shoulders
JCURVE = 0.08                         # m: toward the feet at the chest
SINK, T_SINK = 0.008, 0.25            # a paused touch sinks 8 mm into the chest in 0.25 s
SEEDS = (1, 2, 3)
POST = 17.0                           # s racked after the last clang: the app's SET_GAP (15 s) closes the set

# synth-paused's concentric shapes: (fraction of the time, fraction of the peak speed) knots
SHAPES = {
    "light": [(0.12, 1.0), (0.30, 0.55), (0.66, 1.0)],
    "heavy": [(0.08, 1.0), (0.22, 0.35), (0.55, 0.30), (0.80, 0.75)],
    "grind": [(0.06, 1.0), (0.16, 0.25), (0.55, 0.18), (0.82, 0.70)],
}
DOWN = [(0.45, -1.0)]                 # the descent peaks 45% of the way (synth-paused)
SLOW_START = [(0.10, -0.2), (0.35, -0.2), (0.65, -1.0)]   # a descent eased into: 20% of its peak speed for its
                                      # first third, then the peak (the D43 review's first-hold case)


def seg_disp(T, v0, a0, v1, a1):
    """Displacement of the quintic with cubic-Hermite velocity (v0, a0) -> (v1, a1) over T."""
    return T * ((v0 + v1) / 2 + (a0 - a1) * T / 12)


def shaped(t0, z0, v0, a0, T, z1, v1, a1, shape):
    """Knots after (t0, z0, v0, a0) up to (t0 + T, z1, v1, a1) through shape [(frac, rel)]: interior
    velocities k * rel, accel 0, k solved so the path lands on z1 (synth-paused)."""
    ts = [t0] + [t0 + f * T for f, _ in shape] + [t0 + T]

    def path(k):
        vs = [v0] + [k * u for _, u in shape] + [v1]
        acs = [a0] + [0.0] * len(shape) + [a1]
        zs = [z0]
        for i in range(len(ts) - 1):
            zs.append(zs[-1] + seg_disp(ts[i + 1] - ts[i], vs[i], acs[i], vs[i + 1], acs[i + 1]))
        return vs, acs, zs
    d0, d1 = path(0.0)[2][-1], path(1.0)[2][-1]
    k = (z1 - d0) / (d1 - d0)
    vs, acs, zs = path(k)
    assert abs(zs[-1] - z1) < 1e-12
    return [(ts[i], zs[i], vs[i], acs[i]) for i in range(1, len(ts))]


def rep(te, rom, mcv, bottom, shape=None, hold=0.5, **kw):
    """One rep. te: descent s (from the top it leaves to the chest touch); rom m (lockout to chest);
    mcv m/s; bottom ("tng", a_bot m/s^2) or ("pause", s); shape: None (one smooth quintic), a SHAPES name,
    or with stick=(m off the chest, m/s) a sticking knot; hold: s at lockout after it (0 = a top turn,
    short m below lockout with a_top m/s^2; or drift=(m/s, s): no stop, it eases down out of lockout).
    v_chest: touch-and-go arrival speed (synth-tng: 0.45 light, 0.35 working). dv: the chest a few mm off.
    down: the descent's shape (DOWN; SLOW_START eases out of the hold before it)."""
    return dict(te=te, rom=rom, mcv=mcv, bottom=bottom, shape=shape, hold=hold, **kw)


def _mono(knots, t0, t1, sign, peak=None):
    """The path through knots goes one way (sign) on (t0, t1); with peak: speeds up all the way to it."""
    tf = np.linspace(t0, t1, 3001)[1:-1]
    v = M.through(tf, knots)[1]
    if not (sign * v > 0).all():
        return False
    if peak is not None:
        vp = v[tf <= peak]
        if len(vp) > 1 and (np.diff(sign * vp) < -1e-9).any():
            return False
    return True


def bench(spec, seed=1):
    """A whole bench set as the pod sees it: t, raw accel (m/s^2, with g, sensor frame), gyro (deg/s) and
    the truth. spec keys: reps (list of rep()), unrack (m, default LOCK), racked (s), first_hold (s),
    heavy (bool: harder tremble), rerack ("hard" | "soft" | "fumble"), handle (dict(after=s after the
    last clang, kind="down")), effort (m of 8.5 Hz shake on heavy/grind concentrics), carry_trem (bool)."""
    rng = np.random.default_rng(seed)
    lock = spec.get("unrack", LOCK)
    heavy = spec.get("heavy", False)
    t0 = spec.get("racked", 3.0)
    effort = spec.get("effort", 0.00008)
    ev = dict(racked=(0.0, t0), unrack=(t0, t0 + 0.5), carry=(t0 + 0.35, t0 + 1.15))
    zk = [(0.0, 0.0, 0, 0), (t0, 0.0, 0, 0), (t0 + 0.5, lock, 0, 0), (t0 + 1.15, lock, 0, 0),
          (t0 + 1.32, lock - 0.005, 0, 0), (t0 + 1.5, lock, 0, 0)]
    xmoves = [(t0 + 0.35, 0.8, CARRY)]
    ymoves = [(t0 + 0.35, 0.6, 0.004)]
    t_h0 = t0 + 1.5
    first = spec.get("first_hold", 1.5)
    holds = [(t_h0, t_h0 + first)]
    carries = [(t0 + 0.35, t_h0)]
    chest, efforts, drifts = [], [], []
    zk.append((t_h0 + first, lock, 0, 0))
    state = (t_h0 + first, lock, 0.0, 0.0)            # where the next descent starts: t, z, v, a
    elapsed = 0.0                                     # of the next descent, already gone (a drift)
    ecc_start = state[0]
    out = []
    rows = spec["reps"]
    for k, r in enumerate(rows):
        last = k == len(rows) - 1
        hold = r["hold"]
        drift = r.get("drift")
        turn = (not last) and hold == 0 and not drift
        assert not (last and (hold == 0 or drift)), "the set ends held at lockout"
        z_top = lock - (r.get("short", 0.0) if turn else 0.0)
        z_bot = lock - r["rom"] - r.get("dv", 0.0)
        t_now, z_cur, v_cur, a_cur = state
        te = r["te"] - elapsed
        assert te > 0.3, f"rep {k + 1}: descent too short after its drift"
        bot = r["bottom"]
        a_top = r.get("a_top", 0.0) if turn else 0.0
        if bot[0] == "pause":
            z_touch, v_touch = z_bot + SINK, 2 * SINK / T_SINK
            t_touch = t_now + te
            down = shaped(t_now, z_cur, v_cur, a_cur, te, z_touch, -v_touch, 0.0, r.get("down", DOWN))
            sit = [(t_touch + T_SINK, z_bot, 0, 0), (t_touch + bot[1], z_bot, 0, 0)]
            chest.append((t_touch + T_SINK, t_touch + bot[1]))
            t_start, z_from, v_from, a_from, t_c0 = t_touch + bot[1], z_bot, 0.0, 0.0, t_touch + bot[1]
            info = dict(t_touch=t_touch, pause=bot[1])
        else:
            a_bot = bot[1]
            a_edge = a_bot / 4
            v_edge = min(r.get("v_chest", 0.45), 2 * (z_cur - z_bot - 0.015) / (te - 0.06) / 1.55)
            B = 2 * v_edge / (a_edge + a_bot)
            z_edge = z_bot + B**2 * (2 * a_bot + a_edge) / 6
            t_bot = t_now + te
            down = shaped(t_now, z_cur, v_cur, a_cur, te - B, z_edge, -v_edge, a_edge, r.get("down", DOWN))
            sit = [(t_bot, z_bot, 0, a_bot), (t_bot + B, z_edge, v_edge, a_edge)]
            t_start, z_from, v_from, a_from, t_c0 = t_bot, z_edge, v_edge, a_edge, t_bot + B
            t_touch = t_bot
            info = dict(t_touch=t_bot, a_bot=a_bot, v_chest=v_edge, bounce_half_s=B, compress=z_edge - z_bot)
        rk = z_top - z_bot
        tc = rk / r["mcv"]
        t_top = t_start + tc
        shape = r.get("shape")
        if shape in SHAPES:
            up = shaped(t_c0, z_from, v_from, a_from, t_top - t_c0, z_top, 0.0, a_top, SHAPES[shape])
        else:
            up = []
            st = r.get("stick")
            if st:                                    # synth-edge's sticking knot: (m off the chest, m/s)
                st_h, st_v = st
                t_st = t_c0 + (st_h - (z_from - z_bot)) / ((v_from + st_v) / 2)
                up.append((t_st, z_bot + st_h, st_v, 0.0))
            up.append((t_top, z_top, 0.0, a_top))
        # a top turn's descent starts at its a_top: soften it until that descent speeds up all the way
        # to its peak (synth-tng's cap: else the quintic slows, then drops, a drift of its own)
        if a_cur < 0 and not _mono(zk + down, t_now, down[0][0], -1, peak=down[0][0]):
            for _ in range(30):
                a_cur *= 0.85
                zk[-1] = (zk[-1][0], zk[-1][1], zk[-1][2], a_cur)
                out[-1]["a_top"] = a_cur
                down = shaped(t_now, z_cur, v_cur, a_cur, down[-1][0] - t_now, down[-1][1], down[-1][2], down[-1][3], r.get("down", DOWN))
                if _mono(zk + down, t_now, down[0][0], -1, peak=down[0][0]):
                    break
        zk += down + sit + up
        if shape not in SHAPES and shape is not None:
            raise ValueError(shape)
        if shape in ("heavy", "grind") and effort:
            efforts.append((t_start + 0.1 * tc, t_top - 0.1 * tc))
        d = dict(rep=k + 1, t_start=t_start, t_end=t_top, ecc_start=ecc_start, z_bottom=z_bot, z_top=z_top,
                 bottom=bot[0], shape=shape or "smooth", hold_after=hold, a_top=a_top, **info)
        out.append(d)
        if turn:
            state, elapsed, ecc_start = (t_top, z_top, 0.0, a_top), 0.0, t_top
        elif drift:
            vd, td = drift
            tr = 0.15
            z1 = z_top - vd * tr / 2
            z2 = z1 - vd * (td - tr)
            zk += [(t_top + tr, z1, -vd, 0.0), (t_top + td, z2, -vd, 0.0)]
            drifts.append((t_top, t_top + td))
            d["drift"] = dict(v=vd, s=td)
            state, elapsed, ecc_start = (t_top + td, z2, -vd, 0.0), td, t_top
        else:
            zk.append((t_top + hold, z_top, 0, 0))
            holds.append((t_top, t_top + hold))
            state, elapsed, ecc_start = (t_top + hold, z_top, 0.0, 0.0), 0.0, t_top + hold
    t_last = state[0]
    c0, c1 = t_last, t_last + 0.8                     # the carry back to the uprights
    ev.update(reps=(out[0]["ecc_start"], out[-1]["t_end"]), hold_after=(out[-1]["t_end"], c0), carry_back=(c0, c1))
    xmoves.append((c0, 0.8, -CARRY))
    ymoves.append((c0, 0.6, -0.004))
    carries.append((c0, c1 + 0.1))
    rr = spec.get("rerack", "hard")
    bursts = []                                       # (when, how hard): rings in the accel and the gyro
    spin = (6.0, 2.0)                                 # deg/s kick, deg/s^2 drag: the sleeve after the clang
    if rr == "hard":
        ev["knock"] = c1 - 0.1
        bursts.append((ev["knock"], 0.7))
        t_low0, t_hit, v_hit, t_stop = c1 + 0.1, c1 + 0.5, -0.30, 0.02
        zk += [(t_low0, lock, 0, 0)]
        clangs = [(t_hit + d, s) for d, s in ((0.0, 1.0), (0.12, 1.3), (0.40, 0.8))]
    elif rr == "soft":                                # set in gently: no clang, no knock, no spin
        t_low0, t_hit, v_hit, t_stop = c1 + 0.1, c1 + 0.9, -0.06, 0.06
        zk += [(t_low0, lock, 0, 0)]
        clangs = [(t_hit, 0.05)]
        spin = (0.0, 2.0)
    elif rr == "fumble":                              # synth-edge's miss_bounce: the face, 3 cm out, re-aimed
        ev["knock"] = c1 - 0.05                       # 3 cm above lockout, then lowered in
        bursts.append((ev["knock"], 1.6))
        xmoves += [(c1, 0.25, 0.03), (c1 + 0.55, 0.4, -0.03)]
        up = spec.get("fumble_up", 0.03)                 # m above lockout to re-aim (synth-edge: 3 cm)
        zk += [(c1, lock, 0, 0), (c1 + 0.4, lock + up, 0, 0), (c1 + 1.0, lock + up, 0, 0)]
        ev["fumble"] = (c1, c1 + 1.0)
        t_low0, t_hit, v_hit, t_stop = c1 + 1.0, c1 + 1.45, -0.30, 0.02
        clangs = [(t_hit + d, s) for d, s in ((0.0, 1.0), (0.12, 0.9), (0.42, 1.0), (0.80, 1.4))]
    else:
        raise ValueError(rr)
    zk.append((t_hit, -v_hit * t_stop / 2, v_hit, 0))
    bursts += clangs
    ev.update(rerack=(t_low0, t_hit), clangs=[c for c, _ in clangs])
    t_clang = clangs[-1][0]
    hnd = spec.get("handle")
    total = t_clang + POST + (hnd["after"] if hnd else 0.0)
    n = int(round(total * FS)) + 1
    t = np.arange(n) / FS
    z = M.through(t, zk)
    land = M.landing(t, t_hit, v_hit, t_stop)
    z = tuple(np.where(t >= t_hit, land[i], z[i]) for i in range(3))

    # the pod handled after the rerack (synth-edge 5e): pulled off the end face, lifted 25 cm, turned
    # face-up, then set down on the bench 35 cm lower, still
    hz_, hx_, htilt, hroll = M.track(n), M.track(n), M.track(n), M.track(n)
    if hnd:
        th = t_clang + hnd["after"]
        ev["handle"] = dict(kind=hnd.get("kind", "down"), t=th)
        bursts.append((th, 0.5))
        hz_ = M.steps(t, [(th + 0.05, 0.6, 0.25), (th + 1.6, 0.9, -0.60)])
        hx_ = M.steps(t, [(th + 0.05, 0.6, -0.15), (th + 1.6, 0.9, -0.2)])
        htilt = M.steps(t, [(th + 0.05, 0.6, 70.0), (th + 1.6, 0.9, 20.0)])
        hroll = M.steps(t, [(th + 0.05, 0.6, 40.0)])

    # horizontal: the carries and the J-curve (only inside the reps: z = lock with v = a = 0 at both ends)
    r0, r1 = out[0]["ecc_start"], out[-1]["t_end"]
    inside = (t >= r0) & (t <= r1)
    jc = tuple(np.where(inside, c, 0.0) for c in M.of_depth(z, lock, JCURVE / np.mean([r["rom"] for r in rows]), 0.0))
    x = M.add(M.steps(t, xmoves), jc, hx_)
    y = M.steps(t, ymoves)
    # tremble (the holds and, verify-synth-paused, the carries: a loaded arm is never smooth), breathing on
    # the long holds, hands-on vibration on the chest, effort shake through a hard concentric
    zt, zhz, xt = (0.0010, 6.2, 0.0012) if heavy else (0.0006, 5.5, 0.0008)
    k_tr = spec.get("trem_scale", 1.0)
    zt, xt = zt * k_tr, xt * k_tr
    # (a drift out of lockout trembles like a hold: a smooth one coasts at a steady speed and passes the still test)
    trem = [(a, b) for a, b in holds if b - a > 0.1] + (carries if spec.get("carry_trem", True) else []) + drifts
    rmp = lambda a, b: min(0.15, (b - a) / 2)
    sway_z = M.add(M.track(n), *[M.sway(t, a, b, zt, hz=zhz, ramp=rmp(a, b)) for a, b in trem],
                   *[M.sway(t, a, b, 0.002, hz=0.3, ramp=min(0.5, (b - a) / 2)) for a, b in holds if b - a >= 1.5],
                   *[M.sway(t, a, b, 0.00012, hz=9.5, ramp=0.08) for a, b in chest],
                   *[M.sway(t, a, b, 0.0005, hz=1.3, ramp=min(0.3, (b - a) / 2)) for a, b in chest],
                   *[M.sway(t, a, b, effort, hz=8.5, ramp=0.1) for a, b in efforts])
    sway_x = M.add(M.track(n), *[M.sway(t, a, b, xt, hz=4.6, ramp=rmp(a, b), phase=0.0) for a, b in trem],
                   *[M.sway(t, a, b, 0.0001, hz=10.5, ramp=0.08, phase=0.4) for a, b in chest])
    w2 = lambda hz: (2 * np.pi * hz) ** 2
    ring_z = M.add(M.track(n), *[M.ring(t, tb, s * 22 / w2(23), 23) for tb, s in bursts])
    ring_x = M.add(M.track(n), *[M.ring(t, tb, s * 18 / w2(26), 26) for tb, s in bursts])
    ring_y = M.add(M.track(n), *[M.ring(t, tb, s * 6 / w2(41), 41) for tb, s in bursts])
    pos = [M.add(x, sway_x, ring_x), M.add(y, ring_y), M.add(z, sway_z, ring_z, hz_)]

    # attitude (synth-paused): the sleeve rolls 3 deg at the unrack, 6 deg per m of depth, drifts 4 deg over
    # the set and spins on after the clang (dry friction); the bar tilts 5 deg per m of depth and 1.5 deg
    # more through a hard rep's sticking region; yaw 1 deg with the carries; the clangs ring all three
    deg_per = lambda hz: 1 / (2 * np.pi * hz)
    w0, drag = spin
    tk = clangs[0][0]
    if w0 > 0:
        s_knots = [(0.0, 0.0, 0, 0), (tk, 0.0, 0, 0), (tk + 0.06, w0 * 0.03, w0, -drag),
                   (tk + 0.06 + w0 / drag, w0 * 0.03 + w0 * w0 / (2 * drag) * 0.95, 0, 0)]
        sp = M.through(t, s_knots)
    else:
        sp = M.track(n)
    lin = lambda c: (c * (lock - z[0]) * inside, -c * z[1] * inside, -c * z[2] * inside)
    roll = M.add(M.steps(t, [(t0, 0.5, 3.0), (r0, max(r1 - r0, 1.0), 4.0)]), lin(6.0), sp, hroll,
                 *[M.ring(t, tb, s * 50 * deg_per(29), 29) for tb, s in bursts])
    roll = (roll[0] + 5.0, roll[1], roll[2])
    stick = []
    for r in out:
        if r["shape"] in ("heavy", "grind"):
            dd = r["t_end"] - r["t_start"]
            stick += [(r["t_start"] + 0.15 * dd, 0.6 * dd, 1.5), (r["t_start"] + 0.75 * dd, 0.2 * dd, -1.5)]
    tilt = M.add(lin(5.0), M.steps(t, stick), htilt,
                 *[M.sway(t, a, b, 0.12, hz=zhz, ramp=rmp(a, b), phase=0.5) for a, b in trem],
                 *[M.ring(t, tb, s * 110 * deg_per(37), 37) for tb, s in bursts])
    yaw = M.add(M.steps(t, [(t0 + 0.35, 0.8, 1.0), (c0, 0.8, -1.0)]),
                *[M.sway(t, a, b, 0.10, hz=4.6, ramp=rmp(a, b), phase=2.0) for a, b in trem],
                *[M.ring(t, tb, s * 260 * deg_per(31), 31) for tb, s in bursts])
    phi, phi_rate = -np.radians(roll[0]), -np.radians(roll[1])
    Ry, Rt, Rp = M.rots([0, 0, 1], np.radians(yaw[0])), M.rots([1, 0, 0], np.radians(tilt[0])), M.rots([0, 0, 1], phi)
    R = Ry @ Rt @ M.R_POD @ Rp
    w_world = np.einsum("nji,j->ni", Rt, [0, 0, 1.0]) * np.radians(yaw[1])[:, None] + np.outer(np.radians(tilt[1]), [1, 0, 0])
    w_b = np.einsum("nji,nj->ni", Rp, w_world @ M.R_POD) + np.outer(phi_rate, [0, 0, 1])

    acc_w = np.column_stack([p[2] for p in pos])
    f_s = np.einsum("nji,nj->ni", R, acc_w + np.array([0, 0, G]))
    raw = M.POD_ACC_SCALE * f_s + M.POD_ACC_OFFSET + rng.normal(0, 1, f_s.shape) * M.POD_ACC_NOISE
    raw = np.clip(np.round(raw / M.POD_ACC_LSB) * M.POD_ACC_LSB, -M.POD_ACC_MAX, M.POD_ACC_MAX)
    gyro = np.degrees(w_b) + M.POD_GYRO_BIAS + rng.normal(0, M.POD_GYRO_NOISE, w_b.shape)
    gyro = np.clip(np.round(gyro / M.POD_GYRO_LSB) * M.POD_GYRO_LSB, -M.POD_GYRO_MAX, M.POD_GYRO_MAX)

    # the truth, checked: up all the way, down all the way
    for r in out:
        tf = np.linspace(r["t_start"], r["t_end"], 4001)
        vf = M.through(tf, zk)[1]
        assert (vf[1:-1] > 0).all(), f"rep {r['rep']} doesn't go up all the way"
        td = np.linspace(r["ecc_start"], r["t_touch"] - 1e-6, 4001)
        assert (M.through(td, zk)[1][1:-1] < 0).all(), f"rep {r['rep']}'s descent doesn't go down all the way"
        r.update(rom=r["z_top"] - r["z_bottom"], dur=r["t_end"] - r["t_start"], peak_v=float(vf.max()),
                 t_peak=float(tf[np.argmax(vf)]))
        r["mcv"] = r["rom"] / r["dur"]
        if r["bottom"] == "tng":                      # the bounce as the sensor's vertical sees it
            m = (t > r["t_start"] - 0.15) & (t < r["t_start"] + 0.15)
            r["peak_a_up"] = float(acc_w[m, 2].max())
    a_mag = np.linalg.norm(acc_w + [0, 0, G], axis=1)
    w_mag = np.linalg.norm(np.degrees(w_b), axis=1)
    in_set = (t > t0) & (t < c0)
    after = t >= c0
    info = dict(set_peak_a_up=float(acc_w[in_set, 2].max()), set_peak_abs_a_minus_g=float(np.abs(a_mag[in_set] - G).max()),
                set_peak_gyro=float(w_mag[in_set].max()), rerack_peak_abs_a_minus_g=float(np.abs(a_mag[after] - G).max()),
                rerack_peak_gyro=float(w_mag[after].max()), holds=holds, chest=chest, carries=carries, drifts=drifts)
    return t, raw, gyro, dict(reps=out, events=ev, info=info, lift="bench", lock=lock, total=total,
                              z_knots=[list(map(float, k)) for k in zk], landing=dict(t_hit=t_hit, v_hit=v_hit, t_stop=t_stop))


def build(spec, seed=1):
    """bench(), with spec["bounce_peak"] (m/s^2) honoured: every chest bounce scaled until the highest one
    reads that peak in the sampled vertical acceleration (the vertical the algorithm sees, gravity off)."""
    target = spec.get("bounce_peak")
    if not target:
        return bench(spec, seed)
    sp = dict(spec, reps=[dict(r) for r in spec["reps"]])
    for _ in range(4):
        out = bench(sp, seed)
        peak = max(r.get("peak_a_up", 0.0) for r in out[3]["reps"])
        if abs(peak / target - 1) < 0.002:
            break
        for r in sp["reps"]:
            if r["bottom"][0] == "tng":
                r["bottom"] = ("tng", r["bottom"][1] * target / peak)
    out[3]["info"]["bounce_peak_target"] = target
    return out


# ---- the rows: synth-tng's touch-and-go sets, synth-paused's paused sets, synth-edge's 5-rep base ----

def tng_light(rom=0.32, pause=True, peak=None):
    """synth-tng's light set: 10 reps, MCV 0.85 -> 0.55, chest bounce 13 -> 10 m/s^2 (peak: rep 1 at
    `peak`, falling to 0.85 x it), 0.2 to 0.4 s at lockout (pause) or a top turn 1 to 2.5 cm short."""
    n = 10
    mcv = np.linspace(0.85, 0.55, n)
    te = [0.95, 0.90, 0.95, 1.00, 0.95, 1.00, 1.05, 1.00, 1.10, 1.10]
    dv = [0.000, 0.004, -0.003, 0.002, 0.005, -0.002, 0.003, 0.000, 0.006, 0.004]
    abot = np.linspace(13.0, 10.0, n) if peak is None else peak * np.linspace(1.0, 0.85, n)
    holds = [0.30, 0.25, 0.35, 0.20, 0.30, 0.40, 0.25, 0.30, 0.35, 1.00]
    shorts = [0.015, 0.020, 0.010, 0.025, 0.015, 0.020, 0.010, 0.020, 0.015, 0.0]
    atop = np.linspace(-2.5, -1.8, n)
    return [rep(te[k], rom, float(mcv[k]), ("tng", float(abot[k])), None,
                holds[k] if (pause or k == n - 1) else 0.0, dv=dv[k], short=shorts[k], a_top=float(atop[k]), v_chest=0.45)
            for k in range(n)]


def tng_working(rom=0.32, pause=True, drift=None):
    """synth-tng's working set: 6 reps, MCV 0.60 -> 0.35, sticking knots on reps 5-6 (10-11 cm off the
    chest), 0.6 to 1.0 s at lockout (pause), a top turn (not pause), or a slow drift out of each lockout."""
    mcv = [0.60, 0.55, 0.50, 0.45, 0.40, 0.35]
    te = [1.25, 1.30, 1.35, 1.40, 1.45, 1.50]
    dv = [0.000, 0.003, -0.002, 0.004, 0.002, 0.005]
    abot = [11.0, 10.5, 10.0, 9.0, 8.5, 8.0]
    holds = [0.60, 0.75, 0.90, 1.00, 0.80, 1.00]
    sticks = [None, None, None, None, (0.10, 0.15), (0.11, 0.10)]
    shorts = [0.015, 0.020, 0.010, 0.020, 0.015, 0.0]
    atop = [-1.8, -1.8, -1.6, -1.6, -1.5, 0.0]
    rows = []
    for k in range(6):
        last = k == 5
        kw = dict(dv=dv[k], short=shorts[k], a_top=atop[k], v_chest=0.35, stick=sticks[k])
        if drift and not last:
            kw["drift"] = drift
        rows.append(rep(te[k], rom, mcv[k], ("tng", abot[k]), None, holds[k] if (pause or last) else 0.0, **kw))
    return rows


def paused_rows(pause=1.0, holds=(0.5, 0.8, 1.2, 1.5), last=1.0):
    """synth-paused's bench_paused: 5 reps, MCV 0.50 -> 0.30, a `pause` s chest pause each, `holds` s at
    lockout between them, `last` s after the last."""
    mcvs = [0.50, 0.45, 0.40, 0.35, 0.30]
    roms = [0.380, 0.375, 0.385, 0.370, 0.380]
    tes = [1.0, 1.1, 1.2, 1.3, 1.4]
    shp = ["light", "light", "light", "heavy", "heavy"]
    hs = list(holds) + [last]
    return [rep(tes[k], roms[k], mcvs[k], ("pause", pause), shp[k], hs[k]) for k in range(5)]


def heavy_paused_rows(holds=(2.5, 3.0)):
    """synth-paused's bench_heavy_paused: MCV 0.30, 0.22, 0.15 (a grind), 1.0 s on the chest each."""
    return [rep(1.4, 0.370, 0.30, ("pause", 1.0), "heavy", holds[0]), rep(1.5, 0.365, 0.22, ("pause", 1.0), "heavy", holds[1]),
            rep(1.6, 0.360, 0.15, ("pause", 1.0), "grind", 1.0)]


def base5_rows():
    """synth-edge's control: 5 touch-and-go reps, MCV 0.475 -> 0.247 with a sticking knot 10-11 cm off
    the chest, 0.35 to 1.5 s at lockout, 1.0 s after the last."""
    rows = [(1.10, 0.80, 0.380, 11.0, 0.10, 0.40, 0.35), (1.10, 0.90, 0.380, 11.0, 0.10, 0.33, 0.45),
            (1.20, 1.05, 0.375, 10.0, 0.10, 0.25, 0.80), (1.20, 1.25, 0.375, 9.0, 0.11, 0.18, 1.50),
            (1.30, 1.50, 0.370, 8.0, 0.11, 0.12, 1.00)]
    # synth-edge's bounce ran a fixed 0.07 s each side: arriving at 0.07 x 1.25 a_bot / 2
    return [rep(te, rom, rom / tc, ("tng", ab), None, hold, stick=(sh, sv), v_chest=0.07 * 1.25 * ab / 2)
            for te, tc, rom, ab, sh, sv, hold in rows]


ISSUES = {
    1: "TA_NEAR_FRAC rejects a paused set's first and last holds (rep 1 -38 to -64% with a 0.75 s+ chest pause, "
       "the last rep -29 to -35% after a 1 s+ lockout; 2 s breaths leave 3 of 5)",
    2: "heavy paused grinds and paused singles: 1 of 3 reps, or no card (the grind's top is never a coarse high)",
    3: "TA_RACKED has no gyro check: a chest bounce over ~2 g closes the set and empties the card",
    4: "find_pause takes a slow drift out of lockout for the pause: anchors ~0.3 s late, ROM +5 to +8 cm (also a slow "
       "descent start: the flat run goes on into the moving bar and its mean velocity becomes the zero)",
    5: "level_heights' bench ends rule puts the rack still at lockout height (4 to 6 cm low): with a soft rerack "
       "the window isn't redone and the unrack becomes a rep",
    6: "a rerack fumble above lockout drops the last rep (the first-jolt rule throws away the last pause)",
    7: "the pod taken off before the double buzz loses the last rep",
}

S = {}


def scen(name, issues, note, **spec):
    S[name] = dict(issues=list(issues), note=note, spec=spec)


# touch-and-go: synth-tng's rows (11-01, on synth-tng's generator: 48 of 48 cards right, MCV -8.4 to +7.7%). On this
# generator (its descents start from lockout on synth-paused's smooth 45%-peak profile) four of them fall off issue 4's
# cliff at HEAD: the flat test's run goes on 0.3 to 0.6 s into the slow start of the descent (or a top turn's), find_pause
# takes it for the lockout pause, and its mean velocity (the bar already moving) becomes the zero. Those four pin
# issue 4; the other four are the controls a fix must keep
TNG_PINS = {"tng_working_r32_pause": [4], "tng_light_r32_nopause": [4], "tng_working_r32_nopause": [4],
            "tng_working_r44_nopause": [4]}
for _kind, _fn in (("light", tng_light), ("working", tng_working)):
    for _rom in (0.32, 0.44):
        for _p in (True, False):
            _nm = f"tng_{_kind}_r{round(_rom * 100)}_{'pause' if _p else 'nopause'}"
            scen(_nm, TNG_PINS.get(_nm, []),
                 f"touch-and-go {_kind} set (synth-tng), ROM {_rom * 100:.0f} cm, "
                 + ("a lockout pause after every rep" if _p else "no lockout pause (a top turn short of lockout)")
                 + (" (HEAD: issue 4's mechanism, the flat run into the descent's slow start)" if _nm in TNG_PINS else " (control)"),
                 reps=_fn(_rom, _p), heavy=_kind == "working")
# paused (issue 1): synth-paused's bench_paused at four chest pauses; 0.5 s is the control
for _p in (0.5, 0.75, 1.0, 1.5):
    scen(f"paused_c{round(_p * 100):03d}", [1] if _p > 0.5 else [],
         f"5 paused reps, {_p} s on the chest, 0.5 / 0.8 / 1.2 / 1.5 s at lockout between, 1.0 s after the last"
         + (" (control: 11-01 read it within 2.5%)" if _p == 0.5 else ""), reps=paused_rows(_p))
scen("paused_c100_breath05", [1], "5 paused reps, 1.0 s on the chest, 0.5 s at lockout between and after the last "
     "(the last rep's control: a short last hold read right on 11-01)", reps=paused_rows(1.0, (0.5,) * 4, 0.5))
scen("paused_c100_breath2", [1], "5 paused reps, 1.0 s on the chest, 2.0 s breaths at lockout between them "
     "(11-01: 3 of 5 on the card)", reps=paused_rows(1.0, (2.0,) * 4, 1.0))
# heavy paused (issue 2)
scen("heavy_paused_triple", [1, 2], "synth-paused's heavy paused triple: MCV 0.30 / 0.22 / 0.15 (a 2.4 s grind), 1.0 s "
     "on the chest, 2.5 and 3.0 s breaths, effort shake (11-01: 1 of 3 in 11 of 12 runs)",
     reps=heavy_paused_rows(), first_hold=2.0, heavy=True)
scen("heavy_paused_triple_b15", [1, 2], "the heavy paused triple with 1.5 s breaths (11-01: 2 of 3, the grind lost)",
     reps=heavy_paused_rows((1.5, 1.5)), first_hold=2.0, heavy=True)
scen("paused_single", [2], "one heavy paused single: 1.0 s on the chest, a 2.1 s grind at 0.176 m/s (11-01: no card, 12 of 12)",
     reps=[rep(1.4, 0.37, 0.37 / 2.1, ("pause", 1.0), "grind", 1.0)], first_hold=2.0, heavy=True)
# the hard bounce (issue 3): the light set, rep 1's bounce at the peak, falling to 0.85 x it
for _g in (1.6, 2.0, 2.4):
    scen(f"bounce_{round(_g * 10)}g", {1.6: [], 2.0: [3, 4], 2.4: [3]}[_g],
         f"the light r32 set with lockout pauses, chest bounces peaking at {_g} g ({_g * G:.1f} m/s^2 up, gravity off, as "
         "sampled) on rep 1, falling to 0.85 x it" + {1.6: " (control)", 2.0: " (the edge: just under IMPACT_A 20, the card "
         "must hold; HEAD's reps 8-9 read -17 to -19% by issue 4's mechanism)", 2.4: ""}[_g],
         reps=tng_light(0.32, True, peak=_g * G), bounce_peak=_g * G)
# the 5-rep base and its endings (issues 5, 6, 7)
scen("base5_hard", [], "synth-edge's 5 touch-and-go reps, a 5 cm unrack, a hard (clanging) rerack: the control for 5, 6, 7",
     reps=base5_rows())
scen("base5_soft", [5], "the same set reracked gently (0.06 m/s, no clang): the window isn't redone", reps=base5_rows(), rerack="soft")
scen("unrack4_soft", [5], "a gentle 4 cm unrack and a soft rerack (the code-reader's soft_rerack4)",
     reps=base5_rows(), unrack=0.04, rerack="soft")
scen("unrack6_soft", [5], "a 6 cm unrack and a soft rerack (the code-reader's soft_rerack: 6 for 5, a phantom first rep)",
     reps=base5_rows(), unrack=0.06, rerack="soft")
scen("unrack6_hard", [5], "a 6 cm unrack and a hard rerack: the card is fine, the live view shows the unrack as a rep (11-01)",
     reps=base5_rows(), unrack=0.06)
scen("rerack_fumble", [6], "the rerack hits the upright face, bounces 3 cm out, is lifted 3 cm above lockout to re-aim, then "
     "lowered in (synth-edge's miss_bounce: the last rep lost in 4 of 6 raw runs; a coin flip here too: seeds 1-6 lose it "
     "on cal seed 2 only)", reps=base5_rows(), rerack="fumble")
scen("rerack_fumble_5cm", [6], "the same fumble re-aimed 5 cm above lockout: the coarse high lands in the fumble, after the "
     "knock (the first jolt), so the last pause goes and the last rep with it (seeds 1-6, raw and cal: 4 of 5 every time)",
     reps=base5_rows(), rerack="fumble", fumble_up=0.05)
scen("pod_lift_10s", [7], "the pod pulled off the sleeve 10 s after the rerack, lifted 25 cm and set down on the bench "
     "(synth-edge's v_pick10_down: 4 of 5)", reps=base5_rows(), handle=dict(after=10.0, kind="down"))
# the slow drift out of lockout (issue 4): the working set with no lockout stop, each top easing down
for _rom in (0.32, 0.44):
    scen(f"drift_working_r{round(_rom * 100)}", [4], f"the working set, ROM {_rom * 100:.0f} cm, no lockout stop: after each "
         "top the bar eases down at 0.12 m/s for 0.45 s, then the descent proper (synth-tng's slowstart, made explicit)",
         reps=tng_working(_rom, False, drift=(0.12, 0.45)), heavy=True)
# a long setup (the workflow's stress set): the high-pass bends an 8 s hold before the first rep well down from
# the top, so only the outer exemption (TA_HOLD_V, D43) finds it
scen("single_first8", [1], "a paused single (1.0 s on the chest, a 2.1 s grind at 0.176 m/s) after an 8 s hold at lockout",
     reps=[rep(1.4, 0.37, 0.37 / 2.1, ("pause", 1.0), "grind", 1.0)], first_hold=8.0, heavy=True)
# the workflow's perturbations: the working set's chest bounces 1.25 x harder. Its first hold's flat run reaches into
# the descent's slow start: -16% with neither start rule (TA_START_DROP, D43), right with either
scen("tng_working_r32_pause_hard", [4], "the working set (ROM 32 cm, lockout pauses) with every chest bounce 1.25 x harder",
     reps=[dict(r, bottom=("tng", r["bottom"][1] * 1.25)) for r in tng_working(0.32, True)], heavy=True)
# the D43 review: the first descent eased into out of the setup hold. The hold's flat run goes on into the slow start,
# and its end (where the first anchor averages) is the moving bar: -12% for the single, -30% for the triple's rep 1
scen("paused_single_slowstart", [4], "the paused single, its descent eased into out of the 2 s setup hold (SLOW_START)",
     reps=[rep(1.4, 0.37, 0.37 / 2.1, ("pause", 1.0), "grind", 1.0, down=SLOW_START)], first_hold=2.0, heavy=True)
scen("heavy_paused_triple_slowstart", [4], "the heavy paused triple, its first descent eased into (SLOW_START)",
     reps=[dict(heavy_paused_rows()[0], down=SLOW_START)] + heavy_paused_rows()[1:], first_hold=2.0, heavy=True)
# the workflow's stress set: a touch-and-go heavy triple with long breaths. Each breath's run goes on into the next
# descent, which only the start rule between reps cuts off: -15% without it
scen("tng_heavy_triple", [4], "touch-and-go heavy triple, MCV 0.30 / 0.22 / 0.15, 2.5 and 3.0 s breaths at lockout",
     reps=[rep(1.4, 0.37, 0.30, ("tng", 8.0), "heavy", 2.5, v_chest=0.35), rep(1.5, 0.365, 0.22, ("tng", 8.0), "heavy", 3.0, v_chest=0.35),
           rep(1.6, 0.36, 0.15, ("tng", 8.0), "grind", 1.0, v_chest=0.35)], first_hold=2.0, heavy=True)

SCENARIOS = S

# What parity.mjs checks (D43), each (scenario, seed), raw and cal. Each fails when the rule beside it is reverted
GUARDS = [("paused_c100", 1),            # issue 1 (rep 1 read -48% at HEAD)
          ("paused_single", 1),          # the bench high-pass (TA_SIGMA_LIFT): no card without it
          ("heavy_paused_triple", 1),    # the same: 1 of 3
          ("drift_working_r32", 1),      # TA_HOLD_V between reps: 4 of 6
          ("tng_working_r32_pause", 1),  # issue 4 (reps 1-4 read -9 to -14%)
          ("tng_heavy_triple", 1),      # the start rule between reps: -15%
          ("single_first8", 1),          # the outer exemption: -16% and ROM +7 cm
          ("paused_single_slowstart", 1),        # the first hold's start rule: -12%
          ("heavy_paused_triple_slowstart", 1),  # the same: rep 1 -30%
          ("bounce_24g", 1),             # impacts, not jolts, close the set: an empty card
          ("rerack_fumble_5cm", 2),      # the rack cut: 4 of 5
          ("tng_light_r44_nopause", 1),  # the rack cut, live: a dip at the rerack
          ("unrack6_soft", 1),           # the hooks rules: 6 for 5
          ("pod_lift_10s", 1),           # the closed-set clip: 4 of 5
          ("tng_light_r32_pause", 1),    # controls (the end walk's known low bias on fast reps)
          ("bounce_16g", 1)]


def paths(name, seed, sensor):
    stem = f"fake_bench_{name}_s{seed}_{sensor}"
    return DATA / f"{stem}.csv", DATA / f"{stem}_truth.json"


def write(name, seed=1, sensors=("raw",)):
    """data/fake_bench_<name>_s<seed>_<sensor>.csv (the pod's columns; the lift is in the name) + _truth.json."""
    sc = SCENARIOS[name]
    t, raw, gyro, truth = build(sc["spec"], seed)
    DATA.mkdir(exist_ok=True)
    rnd = lambda o: round(o, 6) if isinstance(o, float) else [rnd(v) for v in o] if isinstance(o, (list, tuple)) else \
        {k: rnd(v) for k, v in o.items()} if isinstance(o, dict) else o
    keys = ("rep", "t_start", "t_end", "dur", "rom", "mcv", "peak_v", "t_peak", "ecc_start", "t_touch", "z_bottom", "z_top",
            "bottom", "pause", "a_bot", "peak_a_up", "v_chest", "shape", "hold_after", "a_top", "drift")
    out = []
    for sensor in sensors:
        acc = raw if sensor == "raw" else (raw - M.POD_ACC_OFFSET) / M.POD_ACC_SCALE
        csv, tj = paths(name, seed, sensor)
        rows = np.column_stack([t, acc, np.linalg.norm(acc, axis=1), gyro])
        np.savetxt(csv, rows, delimiter=",", header=M.HEADER, comments="", fmt="%.6f")
        d = dict(scenario=name, issues=sc["issues"], note=sc["note"], seed=seed, sensor=sensor, lift="bench", fs=FS,
                 samples=len(t),
                 truth_def="exact knot path; a rep: chest turnaround (tng: bounce bottom, v = 0; paused: the press start "
                           "after the pause, v = 0) to lockout (v = 0); heights above the J-hooks; rom = z_top - z_bottom, "
                           "mcv = rom / dur. Tremble, breath, chest vibration, effort shake, rings, handling not in it",
                 reps=[{k: r[k] for k in keys if k in r} for r in truth["reps"]], events=truth["events"],
                 info=truth["info"], lockout_above_hooks=truth["lock"], z_knots=truth["z_knots"], landing=truth["landing"],
                 sensor_model=dict(acc_offset=M.POD_ACC_OFFSET.tolist(), acc_scale=M.POD_ACC_SCALE,
                                   acc_noise=M.POD_ACC_NOISE.tolist(), acc_lsb=M.POD_ACC_LSB, gyro_bias=M.POD_GYRO_BIAS.tolist(),
                                   gyro_noise=M.POD_GYRO_NOISE, gyro_lsb=M.POD_GYRO_LSB, gyro_scale=1.0,
                                   calibrated=sensor == "cal"))
        tj.write_text(json.dumps(rnd(d), indent=1))
        out.append((csv, tj))
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--list" in sys.argv:
        for nm, sc in SCENARIOS.items():
            print(f"{nm:24s} issues {','.join(map(str, sc['issues'])) or '-':5s} {sc['note']}")
        sys.exit(0)
    seeds = next((list(map(int, a.split("=", 1)[1].split(","))) for a in sys.argv if a.startswith("--seeds=")), list(SEEDS))
    sensor = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--sensor=")), "both" if not args else "raw")
    sensors = ("raw", "cal") if sensor == "both" else (sensor,)
    todo = [(nm, sd) for nm in args for sd in seeds] if args else GUARDS
    for nm, sd in todo:
        for csv, tj in write(nm, sd, sensors):
            tr = json.loads(tj.read_text())
            r = tr["reps"]
            print(f"{csv.name}: {len(r)} reps, ROM {' '.join(f'{x['rom'] * 100:.1f}' for x in r)} cm, MCV "
                  f"{' '.join(f'{x['mcv']:.3f}' for x in r)}; set peak a_up {tr['info']['set_peak_a_up']:.1f}, "
                  f"rerack |a-g| {tr['info']['rerack_peak_abs_a_minus_g']:.1f} gyro {tr['info']['rerack_peak_gyro']:.0f}")
