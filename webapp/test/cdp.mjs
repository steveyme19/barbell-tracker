// Shared by the real-Chrome bench checks (pod_browser.mjs, pod_reconnect.mjs): a minimal
// DevTools-protocol client on Node's own WebSocket, Chrome with a throwaway profile, the
// Bluetooth chooser answered through CDP's DeviceAccess domain, and the capture replayed
// through the Python reference decoder (P1). Zero install.

import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function cdp(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0;
  const pending = new Map();
  const handlers = new Map();
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? rej(new Error(`${msg.error.message} ${msg.error.data ?? ""}`)) : res(msg.result);
    } else if (msg.method) {
      for (const h of handlers.get(msg.method) ?? []) h(msg.params);
    }
  };
  const send = (method, params = {}) => new Promise((res, rej) => {
    pending.set(++id, { res, rej });
    ws.send(JSON.stringify({ id, method, params }));
  });
  const on = (method, h) => handlers.set(method, [...(handlers.get(method) ?? []), h]);
  ws.onclose = () => {                            // Chrome died: fail every call still waiting
    for (const { rej } of pending.values()) rej(new Error("Chrome closed the DevTools connection"));
    pending.clear();
  };
  return { send, on, close: () => ws.close() };
}

/**
 * Chrome on url with a throwaway profile, never throttled in the background. Returns
 * { c, js, until, close }: js(expr, gesture) evaluates in the page (awaiting promises),
 * until(expr, ms, what) polls it until truthy.
 */
export async function openChrome({ url, chrome, port = 9333 }) {
  const profile = mkdtempSync(join(tmpdir(), "pod-chrome-"));
  const proc = spawn(chrome, [`--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check", "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding", "--disable-background-timer-throttling", "--new-window", url], { stdio: "ignore" });
  let c = null;
  const close = async () => {
    c?.close();
    proc.kill();
    await sleep(500);
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* Chrome still holds it */ }
  };
  try {
    let page = null;
    for (let i = 0; i < 50 && !page; i++) {
      await sleep(200);
      try {
        const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
        page = list.find((t) => t.type === "page" && t.url.startsWith(url));
      } catch { /* Chrome still starting */ }
    }
    if (!page) throw new Error(`Chrome didn't open ${url}`);
    c = await cdp(page.webSocketDebuggerUrl);
  } catch (e) {
    await close();
    throw e;
  }
  const js = async (expr, gesture = false) => {
    const r = await c.send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true, userGesture: gesture });
    if (r.exceptionDetails) throw new Error(`page: ${r.exceptionDetails.exception?.description ?? r.exceptionDetails.text}`);
    return r.result.value;
  };
  const until = async (expr, ms, what) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      if (await js(expr)) return true;
      await sleep(250);
    }
    throw new Error(`timed out waiting for ${what}`);
  };
  return { c, js, until, close };
}

/** Click Connect pod and answer the Bluetooth chooser with the first BarPod it lists. */
export async function pickPod({ c, js, until }, note = () => {}) {
  await c.send("DeviceAccess.enable");
  let picked = null;
  c.on("DeviceAccess.deviceRequestPrompted", ({ id, devices }) => {
    const d = devices.find((x) => (x.name || "").startsWith("BarPod-"));
    if (d && !picked) {
      picked = d.name;
      c.send("DeviceAccess.selectPrompt", { id, deviceId: d.id }).catch((e) => note(`selectPrompt: ${e.message}`));
    }
  });
  await until("document.readyState === 'complete' && !!window.barDebug", 10000, "the app");
  await js("document.getElementById('podBtn').click()", true);
  await until("document.getElementById('podBtn').textContent.startsWith('Pod: ')", 30000, "the chooser to pick a pod");
  return picked;
}

/** The browser's capture (make_fake_pod.py's vector format) through podlink.py: { samples, gaps,
 * faults }, or { error }. */
export function replayCapture(csv, python, root) {
  const dir = mkdtempSync(join(tmpdir(), "pod-cap-"));
  const csvPath = join(dir, "capture.csv");
  writeFileSync(csvPath, csv);
  const py = spawnSync(python, ["-c", [
    "import csv, json, sys",
    "sys.path.insert(0, sys.argv[2])",
    "import make_fake_pod as m",
    "log = [(r['kind'], float(r['arrival_ms']) if r['arrival_ms'] else None, r['payload']) for r in csv.DictReader(open(sys.argv[1]))]",
    "smp, st, ev = m.replay(log)",
    "print(json.dumps({'samples': smp, 'gaps': st.gaps, 'faults': st.faults}))",
  ].join("\n"), csvPath, join(root, "analysis")], { encoding: "utf8", maxBuffer: 1 << 28 });
  rmSync(dir, { recursive: true, force: true });
  if (py.status !== 0) return { error: py.stderr.slice(-500) };
  return JSON.parse(py.stdout);
}

/** Worst |difference| over t and the six values between two sample lists of the same length. */
export function worstDiff(a, b) {
  let worst = 0;
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    for (const k of ["t", "ax", "ay", "az", "gx", "gy", "gz"]) worst = Math.max(worst, Math.abs(a[i][k] - b[i][k]));
  }
  return worst;
}
