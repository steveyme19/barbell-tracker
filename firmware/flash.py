"""Compile and upload a firmware sketch to the XIAO, after checking the installed core is safe.

    python firmware/flash.py                 # firmware/pod, port found automatically
    python firmware/flash.py imu_serial      # another sketch folder under firmware/
    python firmware/flash.py pod COM14       # explicit port

The guard: Seeed's variant drives P0.14 (VBAT_ENABLE) HIGH at boot, which breaks the battery
rule in CLAUDE.md (P0.14 HIGH while charging can damage P0.31). We patch the installed core to
drive it LOW (firmware/README.md). A core update would silently undo that, so this refuses to
build until the patch is back. Standard library only; uses the arduino-cli inside Arduino IDE 2.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

FQBN = "Seeeduino:nrf52:xiaonRF52840Sense"
HERE = Path(__file__).parent
CLI = Path(os.environ["LOCALAPPDATA"]) / "Programs/Arduino IDE/resources/app/lib/backend/resources/arduino-cli.exe"
CORES = Path(os.environ["LOCALAPPDATA"]) / "Arduino15/packages/Seeeduino/hardware/nrf52"


def check_core():
    """Every installed core version must drive VBAT_ENABLE LOW in initVariant()."""
    bad = []
    variants = list(CORES.glob("*/variants/Seeed_XIAO_nRF52840_Sense/variant.cpp"))
    if not variants:
        sys.exit(f"No Seeed nRF52 core found under {CORES}")
    for v in variants:
        body = v.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"void\s+initVariant\s*\(\s*\)\s*\{(.*?)\n\}", body, re.S)
        if m is None or re.search(r"digitalWrite\s*\(\s*VBAT_ENABLE\s*,\s*HIGH\s*\)", m.group(1)):
            bad.append(v)
    if bad:
        print("REFUSING TO BUILD: this core drives P0.14 (VBAT_ENABLE) HIGH at boot:")
        for v in bad:
            print(f"  {v}")
        print("Fix: in initVariant(), change digitalWrite(VBAT_ENABLE, HIGH) to LOW (firmware/README.md, 'P0.14 at boot').")
        sys.exit(1)
    print(f"core check OK: VBAT_ENABLE driven LOW at boot ({len(variants)} core version(s))")


def find_port():
    out = subprocess.run([str(CLI), "board", "list", "--format", "json"], capture_output=True, text=True).stdout
    data = json.loads(out or "{}")
    ports = data.get("detected_ports", data if isinstance(data, list) else [])
    for p in ports:
        props = p.get("port", {}).get("properties", {})
        if props.get("vid", "").lower() == "0x2886":
            return p["port"]["address"]
    sys.exit("No XIAO found (USB VID 0x2886). Plugged in? If it's asleep, move it or press reset; "
             "if the port is missing, double-tap reset for the bootloader.")


def main():
    sketch = HERE / (sys.argv[1] if len(sys.argv) > 1 else "pod")
    if not sketch.is_dir():
        sys.exit(f"No sketch folder {sketch}")
    check_core()
    port = sys.argv[2] if len(sys.argv) > 2 else find_port()
    print(f"compiling {sketch.name} and uploading to {port} ...")
    r = subprocess.run([str(CLI), "compile", "--fqbn", FQBN, "-u", "-p", port, str(sketch)])
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
