# Enclosure and mounts, v1 (phases 1 to 3)

![layout](layout_v1.png)

One **pod** holds the electronics. Small **adapters** hold the pod on the bar. Every
adapter screws to the same two holes on the pod, so one pod tests both mount spots in
phase 2 (decision O3) and nothing gets redesigned until the final enclosure in 4.4.
All sizes below are first guesses from datasheets. Measure the real parts before
printing anything that has to fit.

## The pod

**Inside, bottom to top:** LiPo, then the XIAO above it, component side up.

- XIAO on top keeps its Bluetooth antenna away from the steel bar (a steel bar right
  next to the antenna eats range), keeps the USB-C port and the LEDs reachable, and
  puts the IMU at a known height.
- LiPo underneath, in its own pocket with ~0.5 mm clearance per side plus a strip of
  foam tape. It must not move, rub on screw tips, or get pressed by the lid.

**Size:** about 39 × 27 × 18 mm outside, 25 to 30 g with the battery.

**Features:**

- **Six flat, square outer faces.** Then the pod itself is the calibration cube for the
  6-position calibration and the full-turn gyro test (roadmap 1.3). Calibrating the
  assembled pod also catches the board sitting slightly crooked in the case. Chamfer the
  edges, don't round the faces.
- **XIAO held square to the walls:** a pocket or two ledges the board drops into, so its
  axes line up with the case faces. Emboss the X/Y/Z arrows on the lid, copied from the
  IMU's axes (the LSM6DS3TR-C datasheet shows them; check against a reading on each face).
- **USB-C slot** in the end wall, so charging and flashing work with the lid on.
- **LED window:** a 1 mm thin spot or a small hole over the XIAO's LEDs (charge LED +
  RGB).
- **Reset pinhole** over the reset button (double-tap enters the bootloader).
- **JST-PH socket** wired to the XIAO's battery pads (underside of the board), with a
  little channel so the wires don't get pinched.
- **Lid:** 4 × M2 screws into heat-set inserts (or self-tapping M2 into plain holes for
  the first print).
- **Adapter interface:** 2 × M3 heat-set inserts in the floor, 20 mm apart, centered
  under the IMU along the long axis. Every adapter is just a plate with two M3 holes.
- **Power:** the XIAO has no switch. For v1, either unplug the JST to turn it off, or
  put a mini slide switch in the battery + lead (then it can't charge while switched
  off). Deep sleep with wake-on-motion is the phase 4 answer (hardware/README.md).

## Adapter A: sleeve end, on the bar axis

- **Puck:** Ø50 × 7 mm disc, 2 or 3 pot magnets (Ø16, countersunk M3) recessed flush in
  the side that touches the bar, pod screwed to the outside face with the **IMU on the
  bar axis**. On the axis there's no lever arm (algorithm.md §3).
- **Only works on a steel end face.** Check your gym's bars with a fridge magnet first:
  some end caps are aluminum or plastic, some have a raised logo or a bolt.
- **Plan B for non-steel ends:** a slip-on cup, inner Ø = measured sleeve Ø + 0.6 mm, 15
  to 20 mm deep, slit on one side and tightened with a small hose clamp, TPU liner
  inside. Needs that much bare sleeve past the plates and collar.
- Sticks out ~25 mm past the sleeve end. Check J-hooks, safeties and the wall.
- The sleeve spins with the plates. That's fine on the axis: the gyro sees it, the
  position doesn't change.

## Adapter B: shaft, just inside the inner collar

- **Saddle:** a block with a 90° V-groove underneath that sits on the shaft. A V on a
  round bar centers itself, so the bar's axis direction is fixed by the part (bar path
  gets its axis for free, D6). TPU or rubber pad in the V so it can't rotate on the
  knurling.
- **Held by** a 20 mm hook-and-loop strap or a small hose clamp through slots in the
  saddle. Magnets on a round shaft only touch along a line and hold poorly; don't trust
  them here.
- **IMU ends up 30 to 35 mm off the axis.** That's the `r` for the lever-arm fix (§3).
  The CAD tells you `r` exactly, so write it down.
- Shaft diameters differ: 28 mm (men's Olympic), 29 mm (power bar), 27 mm (deadlift
  bar), 25 mm (women's). The V handles all of them, but `r` changes, so note which bar.

## Print list

| part | needed for | material | notes |
|---|---|---|---|
| Pod case + lid | 1.3 (as the calibration cube), 1.5, 2.1 | PETG (PLA is fine for a fit-test print) | 4 walls, 30 to 40% infill, floor on the bed |
| Adapter A puck | 2.1 | PETG | magnets glued + screwed in |
| Adapter A slip-on cup | 2.1, only if the end cap isn't steel | PETG + TPU liner | |
| Adapter B saddle | 2.1 | PETG + TPU pad | |
| *Later:* string encoder spool + base | 3.0 (optional) | PETG | see BOM phase 3 |
| *Later:* final enclosure | 4.4 | PETG or tougher | after O3 picks the spot, with the carrier PCB |

For phase 3, stick the printed AprilTag on the pod's flat lid. No extra part.

## Measure before you design (calipers)

- XIAO: length, width, board thickness, height of the tallest part (USB-C), and **where
  the IMU chip sits** (distance from two board edges). Where the antenna is.
- LiPo: length × width × thickness *including* the protection board and where the wires
  come out. Pouch cells vary by a millimeter or two between sellers.
- JST-PH plug and socket bodies.
- Pot magnets and heat-set inserts: check the insert's recommended hole size.
- The gym bar: sleeve Ø, end face (flat? steel? raised cap?), shaft Ø where the saddle
  goes, how much bare sleeve is left past the collar with plates on, J-hook clearance.

## Extra parts (on top of BOM phase 1 and 2)

| item | for |
|---|---|
| M3 heat-set inserts + M3 × 6 to 8 screws | adapter interface |
| M2 heat-set inserts (or self-tapping M2) + screws | lid |
| Pot magnets Ø16 mm, countersunk M3, 3 or 4 | Adapter A |
| Hook-and-loop strap 20 mm wide, or 2 small hose clamps | Adapter B |
| TPU filament, or a scrap of rubber sheet | anti-slip pads |
| Thin foam tape | battery pocket |
| *Optional:* mini slide switch (SS12D00 type) | power |

## CAD

Start parametric so measured numbers drop straight in. Suggested user parameters:

| name | first guess | what |
|---|---|---|
| `xiao_l`, `xiao_w`, `xiao_t` | 21, 17.5, 1.2 mm | XIAO board |
| `xiao_top` | 3.5 mm | tallest part above the board |
| `imu_x`, `imu_y` | measure | IMU center from the board's USB-end corner |
| `batt_l`, `batt_w`, `batt_t` | 33, 20.5, 8.3 mm | LiPo with its protection board |
| `wall`, `floor`, `lid_t` | 2, 2, 2 mm | |
| `fit` | 0.4 mm | clearance per side around parts |
| `m3_insert_d`, `m2_insert_d` | from the insert spec | hole sizes |
| `iface_spacing` | 20 mm | the two M3 inserts |
| `sleeve_d`, `shaft_d` | 50, 28.5 mm | the bar |
| `magnet_d`, `magnet_h` | 16, 4.5 mm | pot magnets |

Two ways to get the model:

- Draw it in Fusion with those as user parameters (Modify → Change Parameters).
- Or have Claude Code on the PC write a **Fusion 360 Python script** that builds it
  (in Fusion: Utilities → Add-Ins → Scripts and Add-Ins, add the script, Run). The
  result is a normal parametric Fusion model you can keep editing by hand.

### Pod v1 script (2026-10-16): `hardware/enclosure/pod_v1/pod_v1.py`

Builds the case + lid as a new parametric design: every size above is a user parameter
(first guesses), plus the extras the model needs. Fusion wants the folder and the script
to share a name, hence `pod_v1/pod_v1.py` + `pod_v1.manifest`. Adapters aren't in it yet.

Changes from the sketch above, and why:

- **The battery slides in from the far end**, through a slot in the end wall, under a
  printed tray. The LiPo (20.5 mm) is wider than the XIAO (17.5 mm), so any ledge that
  holds the board above it blocks a battery dropped in from the top. The tray is the
  XIAO ledge (`ledge_w` under every board edge, a window in the middle for the pads and
  wires) plus a fence that holds the board square. Foam tape keeps the battery put.
- **Thicker floor:** `floor` is `floor_t` (floor() is a Fusion function) and gets
  `iface_extra` = 3.5 mm on top, so the M3 insert holes leave 1 mm under the battery.
  Short M3 x 3 inserts: set `m3_insert_h` = 3 and `iface_extra` = 2.5.
- **USB-C end wall is `end_wall` = 5 mm** so two of the lid screws fit beside the board;
  the other two go in square bosses in the far corners, standing on the tray.
- First guesses give 40.8 x 25.3 x 22.5 mm with the lid (vs. ~39 x 27 x 18 estimated).
- `imu_x/y`, `led_x/y`, `rst_x/y` are placeholders (marked MEASURE): measure from the
  board's USB-end corner, component side up, USB-C pointing left, from the bottom-left
  corner (x to the right, y up).
- **Run in Fusion 2026-10-18:** builds clean (40.8 x 25.3 x 22.5 mm, IMU at 15.9, 12.7 mm from
  the USB-end corner, no failed features). Fusion infers a level/plumb constraint when two
  holes share a coordinate, so the script skips those 3 redundant dimensions and lists them;
  the holes still follow the parameters.
- Not modeled yet: edge chamfers, embossed X/Y/Z arrows, the JST socket (it fits in the
  ~10 x 9 mm space between the fence and the far bosses, over the wire hole).
