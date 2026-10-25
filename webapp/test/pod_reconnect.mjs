// Step 10 bench check: the web app's pod source through dropouts, a reset and a sleep, in real
// Chrome over the DevTools protocol (docs/ble-plan.md step 10). Zero install.
//
//   node webapp/test/pod_reconnect.mjs [--url=http://localhost:8765/webapp/] [--signals=DIR]
//        [--phases=outage10,outage60,reset,sleep] [--truth=analysis/usb_clock.json] [--name=TAG]
//        [--hands-free --cmd-file=PATH]
//
// --hands-free: nobody lifts or touches the pod. The outages run during rest (no set checks);
// the reset is the watchdog (bench command h) and the sleep the dry run w (the whole sleep
// prologue, then a reset: the pod comes back as a new boot), both sent over USB by
// analysis/usb_clock.py through its --cmd-file (it holds the pod's only USB port).
//
// Needs the pod advertising nearby (Bluetooth headphones off, D28) and the app on localhost. The
// phases need hands: the script prints ">>> WAITING <signal>: what to do" and goes on once the
// file DIR/<signal> exists (touch it when the step is done). DIR defaults to a temp folder it prints.
//   outage10: (reps10) start slow reps, keep going ~30 s, then hold still. 8 s in, the link drops
//             for 10 s (barDebug.podOutage). Pass: one set, no gap, no flag, one more reconnect.
//   outage60: (reps60) slow reps ~25 s, then hold still. 10 s in, a 60 s outage: more than the
//             pod's 38 s ring. Pass: one overflow gap counted exactly (= the rise of the pod's own
//             lost_overflow), the set before it flagged on its card, the gap in link.json.
//   reset:    (reset) press the pod's reset button, during rest. Pass: a new epoch, t still rising,
//             and the new epoch placed within 0.2 s of the truth: analysis/usb_clock.py running on
//             the pod's USB port meanwhile (--truth, its JSON) gives each boot's wall-clock origin.
//   sleep:    the SLEEP op: the "Pod asleep" banner; (pickup) pick the pod up: it comes back by
//             itself; (reps) a set after the wake: detected.
// End: End session's STOP handshake, the capture replayed through podlink.py gives the same
// samples and gaps, and link.json + the capture go to analysis/data (P2).

import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { sleep, openChrome, pickPod, replayCapture, worstDiff } from "./cdp.mjs";

const arg = (k, d) => process.argv.find((a) => a.startsWith(`--${k}=`))?.split("=").slice(1).join("=") ?? d;
const URL0 = arg("url", "http://localhost:8765/webapp/");
const CHROME = arg("chrome", "C:/Program Files/Google/Chrome/Application/chrome.exe");
const PHASES = arg("phases", "outage10,outage60,reset,sleep").split(",");
const SIGNALS = resolve(arg("signals", "") || mkdtempSync(join(tmpdir(), "pod-signals-")));
const TRUTH = arg("truth", "");
const NAME = arg("name", "reconnect10");
const HANDS_FREE = process.argv.includes("--hands-free");
const CMD_FILE = arg("cmd-file", "");
const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const PYTHON = process.env.PYTHON || join(ROOT, "analysis", ".venv", "Scripts", "python.exe");

let ok = true;
const check = (good, msg) => { console.log(`${good ? "PASS" : "FAIL"}  ${msg}`); ok &&= Boolean(good); };
const note = (msg) => console.log(`      ${msg}`);

async function signal(name, what) {
  console.log(`>>> WAITING ${name}: ${what}`);
  while (!existsSync(join(SIGNALS, name))) await sleep(200);
  console.log(`      got ${name}`);
}

/** A hand step, or in --hands-free its USB stand-in (cmd, sent by usb_clock.py), or nothing. */
async function act(name, what, cmd = null) {
  if (!HANDS_FREE) return signal(name, what);
  if (!cmd) return;
  if (!CMD_FILE) throw new Error(`--hands-free needs --cmd-file for "${cmd}"`);
  writeFileSync(CMD_FILE, cmd);
  while (existsSync(CMD_FILE)) await sleep(50);     // usb_clock sent it
  note(`sent ${cmd} over USB at ${new Date().toTimeString().slice(0, 8)}`);
}

/**
 * The decoder's placement of epoch e2 after e1 against the pod's USB clock (--truth): each boot's
 * wall-clock origin, from usb_clock.py. That truth is known modulo wrap_s (the pod's u32 t_us), so
 * the nearest wrap is taken: the decoder only has to be within half a wrap (35 min) to pick it, and
 * the check is on what is left.
 */
function placement(e1, e2, what) {
  if (!TRUTH) { note(`no --truth: ${what}'s placement isn't checked against the pod's USB clock`); return; }
  const j = JSON.parse(readFileSync(TRUTH, "utf8"));
  const uniq = [];                                // one entry per USB (re)open: merge reopens of one boot
  for (const b of j.boots) if (!uniq.length || Math.abs(b.base_s - uniq.at(-1).base_s) > 0.05) uniq.push(b);
  const [b1, b2] = uniq.slice(-2);
  const placed = e2.tOffset - e1.tOffset;         // where the decoder put the new epoch
  const mod = b2.base_s - b1.base_s;              // where it is, by the pod's USB stream
  const wraps = Math.round((placed - mod) / j.wrap_s);
  const truth = mod + wraps * j.wrap_s;
  check(uniq.length >= 2 && Math.abs(placed - truth) <= 0.2,
    `${what}: new epoch placed ${placed.toFixed(3)} s after the old one; the USB truth says ${truth.toFixed(3)} s` +
    `${wraps ? ` (${mod.toFixed(3)} + ${wraps} t_us wrap)` : ""}: error ${((placed - truth) * 1000).toFixed(1)} ms`);
}

async function main() {
  console.log(`signals in ${SIGNALS}`);
  const ch = await openChrome({ url: URL0, chrome: CHROME });
  const { js, until } = ch;
  const pod = () => js("window.barDebug.pod()");
  const link = () => js("window.barDebug.link()");
  const nSets = () => js("window.barDebug.session().sets.length");
  const sets = () => js("window.barDebug.session().sets.map((s) => ({ n: s.nReps, startT: s.startT, endT: s.endT, gaps: s.gaps, card: s.card.querySelector('.set-gap').textContent }))");
  const banner = () => js("document.getElementById('podBanner').hidden ? '' : document.getElementById('podBanner').textContent");
  const idxGaps = (L) => L.gapList.filter((g) => !g.kind.startsWith("imu"));
  try {
    const picked = await pickPod(ch, note);
    check(picked, `chooser picked ${picked}`);
    await js(`(() => { const e = document.getElementById('exercise'); e.value = 'Deadlift'; e.dispatchEvent(new Event('change')); })()`);
    await js("window.barDebug.podCapture = true");
    await js("document.getElementById('startBtn').click()", true);
    await until("!document.getElementById('endBtn').disabled || document.getElementById('hint').classList.contains('err')", 20000, "the session to start");
    const startErr = await js("document.getElementById('hint').classList.contains('err') ? document.getElementById('hint').textContent : ''");
    check(!startErr, `session started ${startErr ? `(error: ${startErr})` : ""}`);
    if (startErr) return;
    await sleep(3000);
    note(await js("document.getElementById('rate').textContent"));

    if (PHASES.includes("outage10")) {
      console.log("---- outage10: a 10 s dropout mid-set ----");
      const L0 = await link(), n0 = await nSets();
      await act("reps10", "start slow reps now and keep going about 30 s, then hold still");
      await sleep(8000);
      await js("window.barDebug.podOutage(10000)");
      note(`outage at ${new Date().toTimeString().slice(0, 8)}; banner: "${await banner()}"`);
      await sleep(1500);
      const during = await banner();
      await until("window.barDebug.pod()?.linked", 40000, "the link to come back");
      if (!HANDS_FREE) {
        await until(`window.barDebug.session().sets.length > ${n0}`, 70000, "the set to close");
        await sleep(16000);                       // a second set from the same reps would close by now
      } else {
        await sleep(5000);                        // the backlog drains
      }
      const L1 = await link(), S = (await sets()).slice(n0);
      check(/link lost, reconnecting/.test(during), `banner during the outage: "${during}"`);
      if (!HANDS_FREE) {
        check(S.length === 1 && S[0].n >= 3 && !S[0].gaps?.length && !S[0].card,
          `one set across the dropout: ${S.length} set(s), reps ${S.map((s) => s.n).join(" ")}, flags ${JSON.stringify(S.map((s) => s.card))}`);
      }
      check(idxGaps(L1).length === idxGaps(L0).length && L1.reconnects === L0.reconnects + 1 && !L1.faults.length,
        `no gap (index gaps ${idxGaps(L0).length} -> ${idxGaps(L1).length}), reconnects ${L0.reconnects} -> ${L1.reconnects}, faults ${JSON.stringify(L1.faults)}`);
    }

    if (PHASES.includes("outage60")) {
      console.log("---- outage60: a 60 s dropout mid-set (longer than the ring) ----");
      const L0 = await link(), n0 = await nSets();
      await act("reps60", "start slow reps now, keep going about 25 s, then hold still (the link is down a minute)");
      await sleep(10000);
      const podLost0 = (await pod()).podLost;
      await js("window.barDebug.podOutage(60000)");
      note(`60 s outage at ${new Date().toTimeString().slice(0, 8)}, the pod's lost_overflow ${podLost0}`);
      await until("window.barDebug.pod()?.linked", 120000, "the link to come back");
      if (!HANDS_FREE) await until(`window.barDebug.session().sets.length > ${n0}`, 60000, "the set before the gap to close");
      await sleep(5000);                          // the pod's next STATUS carries the new lost_overflow
      const L1 = await link();
      const fresh = idxGaps(L1).slice(idxGaps(L0).length);
      const rise = L1.podLost - podLost0;
      const odr = L1.epochList.at(-1).odr;
      check(fresh.length === 1 && fresh[0].kind === "overflow" && fresh[0].n === rise && rise > 0,
        `one overflow gap of ${fresh[0]?.n} samples (${(fresh[0]?.n / odr).toFixed(1)} s); the pod's lost_overflow rose ${rise}`);
      if (!HANDS_FREE) {
        const S = (await sets()).slice(n0);
        const flagged = S.filter((s) => /data gap/.test(s.card));
        check(flagged.length >= 1, `the set next to it is flagged: ${JSON.stringify(S.map((s) => s.card || "(no flag)"))}`);
      }
      check(L1.gapList.some((g) => g.kind === "overflow" && g.n === rise), "link.json lists the gap with its count");
    }

    if (PHASES.includes("reset")) {
      console.log("---- reset: the pod restarts mid-rest ----");
      const L0 = await link();
      await act("reset", "press the pod's reset button once (it's resting)", "h");   // hands-free: the watchdog, 8 s on
      await until(`window.barDebug.link().epochList.length > ${L0.epochList.length} && window.barDebug.pod()?.linked`, 60000, "the pod's new epoch");
      await sleep(12000);                         // usb_clock collects the new boot's lines meanwhile
      const L1 = await link();
      const [e1, e2] = L1.epochList.slice(-2);
      check(e2.reboot && e2.bootId !== e1.bootId && L1.stats.t_backwards === 0 && L1.stats.clock_clamped === 0,
        `a new epoch (boot ${e1.bootId} -> ${e2.bootId}), t still rising (t_backwards ${L1.stats.t_backwards}, clamped ${L1.stats.clock_clamped})`);
      placement(e1, e2, "the reset");
    }

    if (PHASES.includes("sleep")) {
      console.log("---- sleep: the SLEEP op, a pickup, back by itself ----");
      const n0 = await nSets();
      const L0 = await link();
      if (HANDS_FREE) await act("sleep", "", "w");   // the dry run: the sleep prologue, then a reset
      else await js("window.barDebug.podSleep(0)");
      await until("!document.getElementById('podBanner').hidden && /asleep/.test(document.getElementById('podBanner').textContent)", 15000, "the asleep banner");
      check(true, `banner: "${await banner()}"`);
      await act("pickup", "pick the pod up (it's asleep) and set it down still");
      const t0 = Date.now();
      await until(`window.barDebug.pod()?.linked && window.barDebug.link().epochList.length > ${L0.epochList.length}`, 120000, "the woken pod");
      const L1 = await link();
      const tail = L1.gapList.slice(L0.gapList.length).filter((g) => g.kind === "sleep-tail");
      check(!(await banner()) && L1.epochList.at(-1).reboot && !tail.length,
        `back by itself as a new boot ${((Date.now() - t0) / 1000).toFixed(1)} s later, banner cleared, sleep-tail ${tail.map((g) => g.n).join(" ") || 0}`);
      if (HANDS_FREE) {                           // the dry run resets the pod: usb_clock sees that boot too
        await sleep(6000);
        const [s1, s2] = (await link()).epochList.slice(-2);
        placement(s1, s2, "the sleep and wake");
      }
      if (!HANDS_FREE) {
        await signal("reps", "do a set: about 5 slow reps, then hold still");
        await until(`window.barDebug.session().sets.length > ${n0}`, 70000, "the set after the wake");
        const S = (await sets()).slice(n0);
        check(S.length >= 1 && S[0].n >= 3, `the next set is detected: reps ${S.map((s) => s.n).join(" ")}`);
      }
    }

    console.log("---- end ----");
    await js("document.getElementById('endBtn').click()", true);
    await until("!document.getElementById('startBtn').disabled", 20000, "End session to finish");
    const L = await link();
    check(L.stats.stopped >= 1 && !L.faults.length, `ended: stopped ${L.stats.stopped}, reconnects ${L.reconnects}, epochs ${L.epochs}, ` +
      `gaps ${JSON.stringify(L.gapList.map((g) => `${g.kind} ${g.n}`))}, faults ${JSON.stringify(L.faults)}`);
    const cap = await js("window.barDebug.podCaptureData()");
    const ref = replayCapture(cap.csv, PYTHON, ROOT);
    if (ref.error) {
      check(false, `python replay failed: ${ref.error}`);
    } else {
      const worst = worstDiff(ref.samples, cap.samples);
      check(ref.samples.length === cap.samples.length && worst < 1e-9 && ref.gaps.length === L.gapList.length,
        `podlink.py replay of the capture: ${ref.samples.length} vs ${cap.samples.length} samples, worst diff ${worst}, gaps ${ref.gaps.length} vs ${L.gapList.length}`);
    }
    const stamp = await js("window.barDebug.session().stamp");
    writeFileSync(join(ROOT, "analysis", "data", `pod-capture-${stamp}-${NAME}.csv`), cap.csv);
    writeFileSync(join(ROOT, "analysis", "data", `pod-weblink-${stamp}-${NAME}.json`), JSON.stringify(L, null, 1));
    note(`saved analysis/data/pod-capture-${stamp}-${NAME}.csv and pod-weblink-${stamp}-${NAME}.json`);
  } finally {
    await ch.close();
  }
}

main().then(() => { console.log(ok ? "PASS" : "FAIL"); process.exit(ok ? 0 : 1); },
  (e) => { console.log(`FAIL  ${e.message}`); process.exit(1); });
