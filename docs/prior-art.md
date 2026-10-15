# Prior art: what's already out there and where it falls short

Researched October 2026. Product facts (prices, status) change fast, so re-check before
quoting them. Accuracy findings come from peer-reviewed studies on PubMed. Some
product sources are vendors writing about competitors; those are marked.

## The products, by sensing method

| type | products | how it works |
|---|---|---|
| Bar IMU | **Enode** (ex-Vmaxpro), Beast Sensor, Bar Sensei → MFX Sport | Accelerometer + gyro on the bar. Same category as ours. |
| Wrist / body IMU | PUSH Band 2.0 | IMU on the forearm (or bar sleeve, or waist) |
| String LPT | **GymAware RS**, Tendo, Vitruve, RepOne, Speed4lifts, T-Force | A cable from a floor unit to the bar. Measures cable length (and angle, on some). |
| Optical | GymAware FLEX | Laser on the bar end, bouncing off a floor mat |
| Phone camera | **Metric VBT**, **Qwik VBT**, My Lift → My Jump Lab | Phone on a tripod beside you, computer vision on the video |
| 3D camera | Perch | Depth camera mounted on the rack, sold to teams |

## Shortcomings, by type

### Old bar and wrist IMUs: the category's bad reputation

- **Beast Sensor and Bar Sensei** were the least valid and least reliable devices in a
  six-device comparison: limits of agreement from -0.36 to +0.46 m/s and coefficients
  of variation up to 120% [1]. Beast is defunct. Bar Sensei was replaced by MFX Sport.
- **PUSH Band 2.0**: higher, unacceptable measurement error and low sensitivity to real
  changes [2]. Mean velocity was not valid on bench at 60 and 90% 1RM because the error
  grew with velocity [3]. Unsuitable at faster velocities and lighter loads [4]. WHOOP
  bought PUSH in 2021 and the service has since been shut down (WHOOP press release;
  GymAware's page on it is a vendor source).
- In forearm mode it measures your arm, not the bar.

**Takeaway:** "IMUs are inaccurate" mostly comes from these early products. It's
not a law of physics, but it is the bar our device gets judged against.

### Enode (ex-Vmaxpro): the best bar IMU, with gaps

- Good for **mean velocity within a session**: treated as an equivalent, cheaper
  alternative to GymAware for mean velocity, but **not** for peak velocity [2].
- **Weaker at slow velocities**: valid at high velocities, only moderately valid at low
  ones [5]. Enode lists its own measuring range as **> 0.15 m/s**. In powerlifters,
  average 1RM velocity was about 0.10 m/s on bench and 0.14 m/s on deadlift [6]. The
  reps that matter most for RPE and 1RM sit at or below its stated range.
- **Systematic bias**: overestimated mean velocity by 0.06 m/s vs. motion capture in the
  deep squat [7]. Another study found it reading lower than a Tendo LPT, with errors up
  to 0.13 m/s [8]. Bias in both directions across studies means you can't just subtract
  a constant.
- **Day-to-day reliability is only moderate** (ICC 0.57 to 0.84) [7]. That's exactly
  what session-to-session comparisons depend on.
- **~65 Hz over Bluetooth** (it samples at 1000+ Hz internally and computes on the
  device). The algorithm is closed, so you can't see how it detects reps or defines
  its metrics.
- Shows bar path (sagittal and frontal). I couldn't find a published validation of that
  bar path.
- €329. The Pro app is free for up to 3 athletes with export. API access starts at
  €39/month.

### String LPTs: most accurate, least convenient

- **GymAware** is the reference device in most comparisons: most reliable, most
  sensitive, smallest bias [1, 2, 9]. **RepOne** matched motion capture in powerlifters
  (RMSE 0.01 to 0.04 m/s) [10].
- **Tethered**: a cable from the floor to the bar, and the unit has to stay put under it.
  Some (Vitruve, by GymAware's vendor claim) have no angle correction, so they assume the
  bar moves straight up and down.
- **Not all LPTs are equal**: Speed4lifts showed more variability at higher loads [9]
  and was unsuitable at faster velocities [4].
- **Cost**: GymAware RS is about $1,995 a unit (SimpliFaster), plus cloud plans of
  $325 to $1,095 a year for storage, CSV export, and API. Vitruve and RepOne are about
  $450 to $590.
- **Bar path**: GymAware yes. Vitruve no. RepOne's advertised 3D motion isn't live yet.

### Optical (GymAware FLEX)

- Poorer validity than LPTs, especially at higher velocities [11]. Needs the floor mat
  placed right with a clear line of sight. About $495.

### Phone camera apps: cheap, best at bar path, picky about setup

- **Missed reps**: in powerlifters, Metric VBT missed 52 of 589 reps and MyLift
  missed 175 [10].
- **Accuracy varies a lot by app**: Qwik VBT was as valid as an LPT [10]. Metric VBT's
  range of motion was off by up to about ±5 cm and its mean velocity agreement was poor
  to moderate [12]. The old My Lift had errors far above acceptable (smallest detectable
  change 0.26 to 0.34 m/s) [13]. Its successor, My Jump Lab, did well on bench [14].
- **Setup**: phone on a tripod, filming from the side, the whole lift in frame. Hard in a
  crowded gym, and accuracy depends on camera angle [12].
- Qwik VBT is analyzed after the set with manual plate tagging, so no live feedback.
  Metric's live feedback and full export need a subscription ($10.99/month or
  $79.99/year on the US App Store).
- Their real strength is that bar path is easy to show and easy to trust visually.

### Perch (3D camera)

- Listed as promising by a 2025 systematic review [15], but sold to teams through
  Catapult with quote-only pricing. Not a product for one lifter.

## Problems shared by nearly all of them

1. **1RM prediction is noisy no matter the sensor.** With two common sensors, estimates
   systematically over- or underestimated the real 1RM, with random scatter from 4% to
   72% depending on the velocity threshold used [8]. A 2026 meta-analysis found high
   average validity, but large heterogeneity for lower-body lifts and too few
   measurement-error analyses to draw firm conclusions [16].
2. **The research behind "validated" is thin.** Only 5 of 66 validity studies met all
   the criteria in a 2025 review framework [15]. Marketing "validated" usually means
   "correlates well", not "small error".
3. **Raw data is locked up.** Most export per-rep summaries at best. Only Vitruve
   Force says outright that it exports raw data. APIs are paywalled (Enode from
   €39/month, GymAware Premium $1,095/year).
4. **Closed algorithms.** You can't see rep detection rules, what counts as the
   concentric, or whether "mean velocity" is MCV or MPV. Different devices disagree
   partly because of different definitions.
5. **Slow, heavy reps are a weak spot.** Enode is less valid at low velocities [5] and
   lists > 0.15 m/s as its range. Even GymAware's mean-velocity reliability dipped at
   100% 1RM in one study [1]. Those are the reps that matter most for RPE and 1RM.

## What this means for our project

- **Realistic target: Enode-level mean velocity.** Beating PUSH and Beast is easy.
  GymAware-level accuracy is the stretch goal. Phase 3 targets should be written against
  these published numbers, not made up.
- **Win on slow reps.** That's where the competition is weakest and where our features
  (RPE, 1RM) need accuracy most. Phase 3.4 already tests heavy reps. Treat them as the
  headline result. Part of the slow-rep problem may be definitional, not sensor
  physics: a velocity threshold for rep boundaries adds a near-fixed m/s error that's
  +19% on a 0.10 m/s rep (`analysis/demos/threshold_bias.py`). Their algorithms are
  closed, so that's a hypothesis about them, but it's a mistake we avoid by design
  (algorithm.md §8).
- **Win on openness.** Raw data for every set, documented algorithm (algorithm.md),
  documented metric definitions. Nobody else does this.
- **Validate bar path for real.** The Smith machine test and video comparison (3.3 to
  3.5) would be more evidence than I could find for any IMU bar path.
- **Day-to-day repeatability matters.** It's Enode's weak spot and it's what
  session-to-session consistency depends on. Test it explicitly (roadmap 3.4).
- **Show uncertainty.** e1RM and RPE as ranges, never one fake-precise number.
- **Cheap extra reference for phase 3:** Qwik VBT matched motion capture for mean
  velocity in powerlifters [10] and exports CSV with bar path. Running it next to video
  is a useful cross-check. Its bar path accuracy hasn't been validated in that study.

---

## References

**From PubMed:**

1. Thompson SW et al. The reliability and validity of current technologies for measuring
   barbell velocity in the free-weight back squat and power clean. *Sports* 2020.
   [doi:10.3390/sports8070094](https://doi.org/10.3390/sports8070094)
2. Jukic I et al. Implementing a velocity-based approach to resistance training: the
   reproducibility and sensitivity of different velocity monitoring technologies.
   *Sci Rep* 2023. [doi:10.1038/s41598-023-34416-0](https://doi.org/10.1038/s41598-023-34416-0)
3. Lake J et al. The reliability and validity of the bar-mounted PUSH Band 2.0 during
   bench press with moderate and heavy loads. *J Sports Sci* 2019.
   [doi:10.1080/02640414.2019.1656703](https://doi.org/10.1080/02640414.2019.1656703)
4. Callaghan DE et al. Validity of the PUSH band 2.0 and Speed4lifts to measure velocity
   during upper and lower body free-weight resistance exercises. *J Sports Sci* 2022.
   [doi:10.1080/02640414.2022.2043629](https://doi.org/10.1080/02640414.2022.2043629)
5. Dragutinovic B et al. Evaluation of the Vmaxpro sensor for assessing movement velocity
   and load-velocity variables. *Biol Sport* 2024.
   [doi:10.5114/biolsport.2024.125596](https://doi.org/10.5114/biolsport.2024.125596)
6. Helms ER et al. RPE and velocity relationships for the back squat, bench press, and
   deadlift in powerlifters. *J Strength Cond Res* 2017.
   [doi:10.1519/JSC.0000000000001517](https://doi.org/10.1519/JSC.0000000000001517)
7. Feuerbacher JF et al. Validity and test-retest reliability of the Vmaxpro sensor for
   evaluation of movement velocity in the deep squat. *J Strength Cond Res* 2023.
   [doi:10.1519/JSC.0000000000004207](https://doi.org/10.1519/JSC.0000000000004207)
8. Warneke K et al. Random measurement and prediction errors limit the practical relevance
   of two velocity sensors to estimate the 1RM back squat. *Front Physiol* 2024.
   [doi:10.3389/fphys.2024.1435103](https://doi.org/10.3389/fphys.2024.1435103)
9. van den Tillaar R et al. Comparison of velocity and estimated one repetition maximum
   measured with different measuring tools in bench presses and squats. *Sensors* 2024.
   [doi:10.3390/s24237422](https://doi.org/10.3390/s24237422)
10. Renner A et al. Concurrent validity of novel smartphone-based apps monitoring barbell
    velocity in powerlifting exercises. *PLoS One* 2024.
    [doi:10.1371/journal.pone.0313919](https://doi.org/10.1371/journal.pone.0313919)
11. Fritschi R et al. Validity and effects of placement of velocity-based training
    devices. *Sports* 2021. [doi:10.3390/sports9090123](https://doi.org/10.3390/sports9090123)
12. Trowell DA et al. Validation of a commercially available mobile application for
    velocity-based resistance training. *PeerJ* 2024.
    [doi:10.7717/peerj.17789](https://doi.org/10.7717/peerj.17789)
13. Martínez-Cava A et al. Reliability of technologies to measure the barbell velocity.
    *PLoS One* 2020. [doi:10.1371/journal.pone.0232465](https://doi.org/10.1371/journal.pone.0232465)
14. Balsalobre-Fernández C et al. Validity of a smartphone app using artificial
    intelligence for the real-time measurement of barbell velocity in the bench press.
    *J Strength Cond Res* 2023. [doi:10.1519/JSC.0000000000004593](https://doi.org/10.1519/JSC.0000000000004593)
15. Wannouch YJ et al. A systematic review using a multi-layered criteria framework for
    assessing the validity and reliability of velocity monitoring devices in resistance
    training. *PLoS One* 2025.
    [doi:10.1371/journal.pone.0324606](https://doi.org/10.1371/journal.pone.0324606)
16. Claassen N et al. Reliability, device agreement and validity of load-velocity
    profiles: a systematic review with meta-analysis. *Sports Med Open* 2026.
    [doi:10.1186/s40798-026-01102-0](https://doi.org/10.1186/s40798-026-01102-0)

**Product pages and other sources:**

- Enode sensor specs (65 Hz BLE, > 0.15 m/s range, €329): https://enode.ai/products/enode-sensor
- Enode pricing: https://enode.ai/pages/pricing
- Enode bar path metrics: https://enode.ai/apps/help/articles/1777540390-available-metrics
- GymAware RS: https://gymaware.com/product/gymaware-rs/
- GymAware price (SimpliFaster): https://store.simplifaster.com/product-category/gymaware/
- GymAware cloud plans: https://gymaware.com/product/gymaware-cloud-pro/
- GymAware FLEX: https://gymaware.com/product/flex-barbell-tracker/ and https://store.simplifaster.com/product/flex/
- Vitruve Force: https://shop.vitruve.fit/products/vitruve-force
- Vitruve raw export: https://vitruve.fit/blog/vitruve-force-the-ultimate-portable-lab-for-vbt-and-jump-analytics/
- GymAware vs Vitruve (vendor source): https://gymaware.com/gymaware-vs-vitruve/
- RepOne Tether: https://www.reponestrength.com/tether?hsLang=en
- WHOOP acquires PUSH: https://www.whoop.com/us/en/press-center/acquires-push-velocity-based-training-solution/
- PUSH shutdown (vendor source): https://gymaware.com/push-band-alternative/
- Bar Sensei → MFX: https://www.movefactorx.com/blogs/assess2perform-blog/why-i-went-with-movefactorx-as-my-choice-for-a-velocity-based-training-solution
- Metric VBT subscriptions: https://metric.coach/docs/subscriptions
- Metric VBT App Store: https://apps.apple.com/us/app/metric-vbt-gym-workout-tracker/id1595510857
- Qwik VBT: https://play.google.com/store/apps/details?id=com.zopfi.qwik_vbt&hl=en_GB
- Perch / Catapult: https://www.catapult.com/perch
