"""
The pod's clock against the PC's, from its USB stream: a truth for where a decoder should put a
new epoch after a reset (docs/ble-plan.md step 10, "the epoch gap within 0.2 s of a stopwatch").

    python usb_clock.py out.json [COMx] [--seconds=N] [--cmd-file=PATH]

Every CSV line t_us arrives on the PC a little after the pod printed it, so per boot the smallest
(PC time - t_us) is that boot's wall-clock origin (when its RTC2 read 0), to about a millisecond
(USB polls every 1 ms; the pod prints every loop pass, ~5 ms). A reset re-enumerates the port: it
reopens and starts the next boot. out.json is rewritten every 2 s:
  {"clock": "perf_counter", "boots": [{"base_s", "lines", "first_t_us", "last_t_us"}]}
Only differences between bases mean anything (perf_counter is the PC's monotonic clock), and that
is what a decoder's epoch offsets are compared with: t_offset2 - t_offset1 = base2 - base1.
The pod prints t_us as a u32 (it wraps every 2^32 us = 71.6 min): wraps while this runs are
counted, but a boot that had already wrapped before the port opened has a base off by whole
wraps, so compare bases modulo wrap_s (seen 2026-10-24: a 2 h old boot, one wrap).
--cmd-file: this holds the pod's only USB port, so it also sends its bench commands: when PATH
appears, its bytes go to the pod (e.g. h: hang, the watchdog resets it) and PATH is removed.
"""

import json
import sys
import time
from pathlib import Path

import serial
import serial.tools.list_ports as lp


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
    boots = []
    sends = []                                    # {"pc", "cmd"}: the bench commands sent
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
            s = serial.Serial(name, 115200, timeout=0.2)
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
                    cmd_file.unlink()
                    s.write(cmd)
                    sends.append({"pc": time.perf_counter(), "cmd": cmd.decode("ascii", "replace")})
                    write()
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
    write()
    print(f"{len(boots)} boots: " + ", ".join(f"base {b['base_s']:.4f} s ({b['lines']} lines)" for b in boots))


if __name__ == "__main__":
    main()
