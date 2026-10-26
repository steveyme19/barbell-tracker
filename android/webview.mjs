// Shared by the phone checks (podlink_check.mjs, pod_session_check.mjs): adb, the app's WebView
// over the DevTools protocol (a debug build's WebView can be inspected), and a bench command to the
// pod over the laptop's USB. Zero install.

import { execFileSync, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

export const APP = "io.github.steveyme19.bartracker";
const HERE = dirname(fileURLToPath(import.meta.url));
const PYTHON = process.env.PYTHON || join(HERE, "..", "analysis", ".venv", "Scripts", "python.exe");
const sdkAdb = join(process.env.LOCALAPPDATA ?? "", "Android", "Sdk", "platform-tools", "adb.exe");
export const adb = (...args) => execFileSync(existsSync(sdkAdb) ? sdkAdb : "adb", args, { encoding: "utf8" }).trim();
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Install the debug APK (the caller builds it first) and open the app fresh. */
export async function install() {
  const out = adb("install", "-r", join(HERE, "app", "build", "outputs", "apk", "debug", "app-debug.apk")).split("\n").at(-1);
  adb("shell", "am", "force-stop", APP);
  adb("shell", "am", "start", "-n", `${APP}/.MainActivity`);
  await sleep(3000);
  return out;
}

/** The app's page over CDP: { js(expr), close() }. js awaits promises and returns values. */
export async function page(port = 9334) {
  let pid = "";
  for (let i = 0; i < 40 && !pid; i++) { try { pid = adb("shell", "pidof", APP); } catch { /* not up yet */ } if (!pid) await sleep(250); }
  if (!pid) throw new Error("Bar Tracker isn't running on the phone.");
  adb("forward", `tcp:${port}`, `localabstract:webview_devtools_remote_${pid}`);
  let pages = [];
  for (let i = 0; i < 40 && !pages.length; i++) {
    try { pages = (await (await fetch(`http://127.0.0.1:${port}/json`)).json()).filter((p) => p.type === "page"); } catch { /* WebView starting */ }
    if (!pages.length) await sleep(250);
  }
  if (!pages.length) throw new Error("No WebView page to inspect (a debug build?).");
  const ws = new WebSocket(pages[0].webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  let id = 0;
  const waiting = new Map();
  ws.onmessage = (m) => { const d = JSON.parse(m.data); waiting.get(d.id)?.(d); waiting.delete(d.id); };
  const js = (expr) => new Promise((res, rej) => {
    const n = ++id;
    waiting.set(n, (d) => (d.result?.exceptionDetails ? rej(new Error(d.result.exceptionDetails.exception?.description ?? "page error"))
      : res(d.result?.result?.value)));
    ws.send(JSON.stringify({ id: n, method: "Runtime.evaluate", params: { expression: expr, awaitPromise: true, returnByValue: true, userGesture: true } }));
  });
  return { js, close: () => { ws.close(); adb("forward", "--remove", `tcp:${port}`); } };
}

/** A bench command to the pod on the laptop's USB (the pod reads one only while a host holds its
 * port open, so this waits for its "# sleeping" answer, up to 3 s). Returns that line. */
export function sendToPod(cmd) {
  const code = ["import serial, time, serial.tools.list_ports as lp",
    "port = next(p.device for p in lp.comports() if '2886' in p.hwid)",
    "s = serial.Serial(port, 115200, timeout=0.3); s.reset_input_buffer(); time.sleep(0.3); s.write(b'" + cmd + "')",
    "t0 = time.time()",
    "while time.time() - t0 < 3:",
    "    ln = s.readline().decode('ascii', 'ignore').strip()",
    "    if ln.startswith('# sleeping'): print(ln); break",
    "s.close()"].join("\n");
  const r = spawnSync(PYTHON, ["-c", code], { encoding: "utf8" });
  if (r.status !== 0) throw new Error(`couldn't send ${cmd} to the pod over USB: ${r.stderr.slice(-300)}`);
  return r.stdout.trim();
}
