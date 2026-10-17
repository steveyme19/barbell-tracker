# Hardware

The parts list for every phase is in [BOM.md](BOM.md). The printed pod and bar
adapters for phases 1 to 3 are in [enclosure.md](enclosure.md) (one pod, two adapters,
sketch in `layout_v1.png`). The final enclosure and carrier PCB start at phase 4.

Planned layout:

```
hardware/
  pcb/         KiCad project (carrier board)
  enclosure/   Fusion 360 exports (.f3d, .step, .stl)
```

## Carrier board (decision D5)

The XIAO gets soldered onto the carrier by its castellated pads. No chip-down
nRF52840, no custom antenna. The carrier handles:

- **Battery:** connector for the LiPo, routed to the XIAO's battery pads. Those are on
  the XIAO's underside, not the edge, so the footprint needs matching pads under it.
- **Power plan** (decide in phase 4): a physical switch in the battery line (simple, but
  then it can't charge while off), or no switch and deep sleep with wake-on-motion from
  the IMU's interrupt pin.
- **Mounting:** holes and outline that match the enclosure.
- **IMU position:** know exactly where the IMU sits relative to the mount face. If the
  chosen mount is off the bar axis, that offset is the `r` in lever-arm compensation
  (algorithm.md §3).
- Copper keepout under the XIAO's antenna (check Seeed's docs for exactly where it is).

## Enclosure

- Designed for the mount spot picked in roadmap 3.7 (decision O3).
- The magnet seat has to fix which way the device faces along the bar (flat end face,
  or a V-groove on the shaft). That gives bar path its bar axis by design
  (algorithm.md §11), so the roll gesture becomes a backup instead of a requirement.
- Magnets are fine: the IMU has no magnetometer.
- Has to survive drops and plate impacts.
