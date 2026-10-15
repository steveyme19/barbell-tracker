# Hardware list (BOM), by roadmap step

Prices checked October 2026 and will drift. **est.** means a generic part I didn't
price-check. Buy each phase's stuff when you get to that phase, not all up front.
Plans change, and phase 4 depends on what phase 3 finds.

## Tools (you probably have most of these)

| item | first needed | notes |
|---|---|---|
| Tape measure | 0.1 | to the mm |
| Soldering iron + solder | 1.5 | battery wires, connectors |
| Multimeter | 1.5 | **check battery polarity before plugging anything in** |
| Calipers | 4.4 | measuring the bar, sleeve, and magnets for the Fusion design |
| 3D printer access | 1.3 | campus makerspace or a friend's printer |

## Phase 0: phone prototyping

| item | step | price | notes |
|---|---|---|---|
| Your Android phone + phyphox | 0.1 to 0.5 | free | |
| PVC pipe or empty bar + tape | 0.6 (optional) | est. a few $ | for fake squats |

**Phase 0 total: ~$0**

## Phase 1: hardware prototype

| item | step | price | notes |
|---|---|---|---|
| Seeed XIAO nRF52840 Sense, **pre-soldered headers** | 1.1 | $16.90 (Seeed) | Pre-soldered so it drops into a breadboard. The regular one is $15.90. |
| USB-C cable, **data-capable** | 1.1 | have one? | Some USB-C cables only charge. If the board never shows up on the laptop, suspect the cable first. |
| Breadboard + jumper wires | 1.1 | est. a few $ | |
| 3D-printed calibration cube | 1.3 | filament | Holds the XIAO square so each face can sit flat for the 6-position calibration and the full-turn gyro test. Design it in Fusion. |
| 3.7 V LiPo, ~400 mAh, **with protection circuit** | 1.5 | $9.15 each at Jameco (Adafruit #3898, sold in pairs) | JST-PH connector. Protection circuit cuts off at 3.0 V and blocks shorts. A spare is handy. |
| JST-PH 2-pin socket with pigtail wires | 1.5 | est. a few $ | The XIAO has battery solder pads, not a connector. Solder a socket on so the battery unplugs. |
| *Optional:* TF-Luna lidar | 1.6 | $30.99 (Waveshare) | UART or I2C, 3.3 V logic (works with the XIAO directly). Needs a **3.7 to 5.2 V supply**, so power it from the XIAO's 5 V pin while on USB for the bench test. |

**Phase 1 total: ~$45 core (XIAO + a pair of LiPos + small parts), +$31 for the laser**

Battery life estimate: the XIAO streaming IMU data probably draws something like
5 to 10 mA, so ~2 days of streaming on 400 mAh. Measure it in 1.5.

## Phase 2: real bar

| item | step | price | notes |
|---|---|---|---|
| Hook-and-loop straps + zip ties | 2.1 | est. a few $ | temporary mount on the shaft |
| Neodymium pot (cup) magnets, a few | 2.1 | est. a few $ each | temporary mount on the sleeve end (O3). Pot magnets hold far harder than plain disc magnets of the same size. |
| 3D-printed temporary case | 2.1 | filament | XIAO + LiPo in one piece so nothing flaps around. **Nothing loose on a loaded bar.** |
| *Optional (2.8):* Pololu 5 V step-up regulator U1V11F5 | 2.8 | $7.95 (Pololu) | Runs the TF-Luna from the LiPo: 0.5 to 5.5 V in, 12 × 15 mm. Its SHDN pin can switch the laser off between sets. |

With the laser on, battery draw jumps to roughly 120 mA (70 mA at 5 V through a boost
converter), so ~3 h of streaming on 400 mAh. That's why the shutdown pin matters.

**Phase 2 total: ~$15 core, +$8 if you try the laser on the bar**

## Phase 3: validation

| item | step | price | notes |
|---|---|---|---|
| Phone that films **240 fps** slow-mo | 3.1 | have it? | Check your phone's camera settings. |
| Phone tripod | 3.1 | est. ~$15 to 25, or borrow | |
| Printed AprilTag | 3.3 | paper | Measure its printed size with calipers. That size is your scale. |
| Smith machine | 3.5 | gym | |
| *Optional 3.0 string encoder:* Adafruit AS5600 breakout | 3.0 | $5.95 (Adafruit #6357) | I2C, STEMMA QT. **No magnet included.** |
| ...diametrically magnetized disc magnet (~6 mm × 2.5 mm) | 3.0 | est. a few $ | Has to be *diametric* (N and S on opposite sides of the round edge, not the flat faces). Many cheap AS5600 kits include one. |
| ...2 × 608 skateboard bearings | 3.0 | est. a few $ | spool axle |
| ...braided Dyneema fishing line | 3.0 | est. ~$10 | almost no stretch |
| ...spring return | 3.0 | est. a few $ | A retractable key reel (pick one with Kevlar cord) or a constant-force spring |
| ...a second microcontroller | 3.0 | have one? | Any Arduino-type board over USB. Sync it with the bar device using the bar tap from 3.2. |
| ...3D-printed spool + base, and a plate to weigh it down | 3.0 | filament | |

**Phase 3 total: ~$0 to 25 core, +$25 to 40 for the string encoder**

## Phase 4: final hardware

| item | step | price | notes |
|---|---|---|---|
| Second XIAO nRF52840 Sense, **unsoldered** | 4.5 | $15.90 (Seeed) | The carrier board solders it down by its castellated edge pads, so headers would be in the way. Keep the pre-soldered one as your dev board. |
| Carrier PCB, fab'd | 4.5 | est. a few $ + shipping | Shipping usually costs more than the boards. |
| JST-PH connector, slide switch or button, LED, resistors | 4.5 | est. a few $ | depends on the power plan (switch vs. wake-on-motion) |
| Pot magnets sized for the final mount | 4.4 | est. a few $ | size from phase 2 testing |
| Filament (PETG or tougher) + M2/M3 heat-set inserts + screws | 4.4 | est. ~$10 to 20 | it'll get knocked around |
| *If O8 says yes:* TF-Luna + boost converter | 4.5 | already bought in 1.6 / 2.8 | |

**Phase 4 total: ~$40 to 60**

## Phase 5: analytics

No new hardware.

## Grand total (rough)

| path | about |
|---|---|
| Core (IMU only) | **$100 to 150** spread over the whole project |
| Plus the optional laser + string encoder | +$65 to 80 |

## Safety notes

- **LiPo on a barbell:** use a protected cell, enclose it fully, never puncture or crush
  it, and don't charge it unattended. A drop or a crushed case is what makes them
  dangerous.
- **Battery polarity:** JST-PH polarity isn't the same across all sellers. Check with a
  multimeter before plugging a battery in.
- **Anything mounted on a loaded bar** has to be secure. A case that flies off during a
  deadlift lockout is a projectile.

## Sources

- XIAO nRF52840 Sense: https://www.seeedstudio.com/Seeed-XIAO-BLE-Sense-nRF52840-p-5253.html
- XIAO nRF52840 Sense (pre-soldered): https://www.seeedstudio.com/Seeed-Studio-XIAO-nRF52840-Sense-Pre-Soldered-p-6330.html
- 400 mAh LiPo (Adafruit #3898 at Jameco): https://www.jameco.com/z/3898-Adafruit-Industries-Lithium-Ion-Polymer-Battery-Ideal-for-Feathers-3-7V-400mAh_2515735.html
- TF-Luna (Waveshare): https://www.waveshare.com/TF-Luna-LiDAR-Range-Sensor.htm
- Pololu U1V11F5: https://www.pololu.com/product/2562
- Adafruit AS5600: https://www.adafruit.com/product/6357
