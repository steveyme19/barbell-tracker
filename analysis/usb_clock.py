"""
The pod's clock against the PC's, from its USB stream: a truth for where a decoder should put a
new epoch after a reset (docs/ble-plan.md step 10, "the epoch gap within 0.2 s of a stopwatch").

    python usb_clock.py out.json [COMx] [--seconds=N] [--cmd-file=PATH] [--samples=PREFIX]
    python usb_clock.py --selftest        # the --cmd-file rules on a stub port, no hardware

Every CSV line t_us arrives on the PC a little after the pod printed it, so per boot the smallest
(PC time - t_us) is that boot's wall-clock origin (when its RTC2 read 0), to about a millisecond
(USB polls every 1 ms; the pod prints every loop pass, ~5 ms). A reset re-enumerates the port: it
reopens and starts the next boot. out.json is rewritten every 2 s:
  {"clock": "perf_counter", "wrap_s", "boots": [{"base_s", "lines", "first_t_us", "last_t_us"}],
   "sends": [{"pc", "cmd"}]}
Only differences between bases mean anything (perf_counter is the PC's monotonic clock), and that
is what a decoder's epoch offsets are compared with: t_offset2 - t_offset1 = base2 - base1.
The pod prints t_us as a u32 (it wraps every 2^32 us = 71.6 min): wraps while this runs are
counted, but a boot that had already wrapped before the port opened has a base off by whole
wraps, so compare bases modulo wrap_s (seen 2026-10-24: a 2 h old boot, one wrap).
--cmd-file: this holds the pod's only USB port, so it also sends its bench commands: when PATH
appears, its bytes go to the pod (e.g. h: hang, the watchdog resets it) and PATH is removed once
they are sent.
--samples: also every sample, one CSV per boot (PREFIX-b1.csv, -b2.csv, ...; the boot's
"samples_csv"), in log_serial.py's columns but with t = the pod's own t_us (wraps unwrapped) in
seconds: the truth compare_ble_usb.py checks a Bluetooth recording against (the step 12 soak).
"""

import json
import sys
import time
from pathlib import Path

import serial
import serial.tools.list_ports as lp

from log_serial import HEADER


WRAP_US = 2 ** 32                                 # the pod's t_us is a u32


def find_port():
    for p in lp.comports():
        if "2886" in p.hwid:                      # Seeed's USB vendor ID
            return p.device
    return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    out = Path(args[0])
    port_name = args[1] if len(args) > 1 else None
    seconds = next((float(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--seconds=")), None)
    cmd_file = next((Path(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--cmd-file=")), None)
    samples = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--samples=")), None)
    csv = None                                    # this boot's samples file (--samples)
    boots = []
    sends = []                                    # {"pc", "cmd"}: the bench commands sent
    sent = None                                   # a command sent whose file wouldn't go yet (still held open)
    t_end = None if seconds is None else time.perf_counter() + seconds
    last_write = 0.0

    def write():
        out.write_text(json.dumps({"clock": "perf_counter", "wrap_s": WRAP_US / 1e6, "boots": boots, "sends": sends}, indent=1))

    while t_end is None or time.perf_counter() < t_end:
        name = port_name or find_port()
        if name is None:
            time.sleep(0.05)
            continue
        try:
            s = serial.Serial(name, 115200, timeout=0.2, write_timeout=1.0)   # no timeout: a write to a port that went can hang (D45)
        except (serial.SerialException, OSError):
            time.sleep(0.05)
            continue
        boot = None                               # a new boot starts at each (re)open
        prev = None
        wraps = 0
        try:
            while t_end is None or time.perf_counter() < t_end:
                if cmd_file is not None and cmd_file.exists():
                    cmd = cmd_file.read_bytes()
                    if cmd and cmd != sent:           # empty: its writer isn't done yet, next pass
                        if s.write(cmd) != len(cmd):  # pyserial returns short, without raising, on an aborted write
                            raise serial.SerialException("short write")   # the file stays: sent after the reopen
                        sent = cmd
                        sends.append({"pc": time.perf_counter(), "cmd": cmd.decode("ascii", "replace")})
                        write()
                    if cmd:
                        try:
                            cmd_file.unlink()         # gone = sent (pod_reconnect.mjs waits for that)
                            sent = None
                        except OSError:
                            pass                      # still held by its writer: only the unlink again, never the send
                elif cmd_file is not None:
                    sent = None                       # someone else removed it: the next file is a new command
                raw = s.readline()
                pc = time.perf_counter()
                ln = raw.decode("ascii", "ignore").strip()
                parts = ln.split(",")
                if len(parts) == 7 and parts[0].isdigit():
                    t_us = int(parts[0])
                    if prev is not None and t_us < prev:
                        if prev - t_us > WRAP_US // 2:
                            wraps += 1                     # the u32 wrapped: same boot
                        else:
                            boot = None                    # the pod restarted without the port going away
                            wraps = 0
                    prev = t_us
                    t_us += wraps * WRAP_US
                    if boot is None:
                        boot = {"base_s": pc - t_us / 1e6, "lines": 0, "first_t_us": t_us, "last_t_us": t_us}
                        boots.append(boot)
                        if samples is not None:
                            if csv is not None:
                                csv.close()
                            path = Path(f"{samples}-b{len(boots)}.csv")
                            csv = open(path, "w", newline="", buffering=1)     # line by line: a kill keeps it
                            csv.write(HEADER + "\n")
                            boot["samples_csv"] = path.name
                    if csv is not None:
                        ax, ay, az, gx, gy, gz = (float(p) for p in parts[1:])
                        csv.write(f"{t_us / 1e6:.6f},{ax:.4f},{ay:.4f},{az:.4f},{(ax * ax + ay * ay + az * az) ** 0.5:.4f},"
                                  f"{gx:.3f},{gy:.3f},{gz:.3f}\n")
                    boot["base_s"] = min(boot["base_s"], pc - t_us / 1e6)
                    boot["lines"] += 1
                    boot["last_t_us"] = t_us
                if pc - last_write > 2:
                    last_write = pc
                    write()
        except (serial.SerialException, OSError):
            pass                                  # the port went away (a reset): reopen
        finally:
            try:
                s.close()
            except Exception:
                pass
    if csv is not None:
        csv.close()
    write()
    print(f"{len(boots)} boots: " + ", ".join(f"base {b['base_s']:.4f} s ({b['lines']} lines)" for b in boots))


def selftest():
    """The --cmd-file rules against a stub port (no hardware): the port is opened with a write
    timeout, a command is sent exactly once and its file removed only after that, and a file that
    can't be removed yet (its writer still holds it, or it is read-only) is never sent twice and
    never taken for the port going away. Each has been a bug here (11-10)."""
    import contextlib
    import io
    import os
    import stat
    import tempfile
    import threading

    class StubPort:                               # a pod: a CSV line every 5 ms, every write kept
        opens, written, timeouts = 0, [], []

        def __init__(self, name, baud, timeout=None, write_timeout=None):
            StubPort.opens += 1
            StubPort.timeouts.append(write_timeout)
            self.t = 0

        def readline(self):
            time.sleep(0.005)
            self.t += 4700
            return f"{self.t},0.1,0.2,9.8,0.0,0.0,0.0\n".encode()

        def write(self, b):
            StubPort.written.append(bytes(b))
            return len(b)

        def close(self):
            pass

    def held(cmd):                                # its writer keeps the file open 300 ms
        time.sleep(0.2)
        with open(cmd, "wb", buffering=0) as f:
            f.write(b"w")
            time.sleep(0.3)

    def read_only(cmd):                           # a file that can't be removed at all
        time.sleep(0.2)
        cmd.write_bytes(b"h")
        os.chmod(cmd, stat.S_IREAD)

    def three(cmd):                               # the same command three times, each its own file
        for _ in range(3):
            time.sleep(0.2)
            cmd.write_bytes(b"i")
            while cmd.exists():
                time.sleep(0.01)

    ok = True

    def check(good, msg):
        nonlocal ok
        print(f"{'PASS' if good else 'FAIL'}  {msg}")
        ok = ok and bool(good)

    real, argv = serial.Serial, sys.argv
    serial.Serial = StubPort
    try:
        for seconds, feeder, want, gone, what in (
                (1.5, held, [b"w"], True, "a file its writer still holds"),
                (1.0, read_only, [b"h"], False, "a read-only file"),
                (1.5, three, [b"i"] * 3, True, "the same command three times")):
            sys.argv = ["usb_clock.py", "", "COM99", f"--seconds={seconds}"]
            folder = Path(tempfile.mkdtemp(prefix="usb_clock-"))
            sys.argv[1] = str(folder / "out.json")
            sys.argv.append(f"--cmd-file={folder / 'cmd'}")
            StubPort.opens, StubPort.written, StubPort.timeouts = 0, [], []
            feed = threading.Thread(target=feeder, args=(folder / "cmd",))
            feed.start()
            with contextlib.redirect_stdout(io.StringIO()):
                main()
            feed.join()
            sends = [s["cmd"] for s in json.loads((folder / "out.json").read_text())["sends"]]
            check(StubPort.written == want and sends == [w.decode() for w in want] and StubPort.opens == 1
                  and (folder / "cmd").exists() != gone and StubPort.timeouts == [1.0],
                  f"{what}: sent {len(StubPort.written)} time(s), logged {sends}, the port opened {StubPort.opens} time(s) "
                  f"with write_timeout {StubPort.timeouts[0]}, the file {'gone' if not (folder / 'cmd').exists() else 'still there'}")
    finally:
        serial.Serial, sys.argv = real, argv
    print("PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    main()
