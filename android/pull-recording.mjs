// Pull the last recorded set out of the Android app into analysis/data/, over USB.
// Stopgap until the app can save files itself (docs/android.md A3).
//
//   node android/pull-recording.mjs
//
// Needs: the app open on the phone (a debug build, so its WebView can be inspected),
// USB debugging on, and the set stopped. Uses Chrome's DevTools protocol through adb.
import { execFileSync } from "node:child_process";
import { existsSync, writeFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const APP = "io.github.steveyme19.bartracker";
const PORT = 9333;
const sdkAdb = join(process.env.LOCALAPPDATA ?? "", "Android", "Sdk", "platform-tools", "adb.exe");
const adb = (...args) => execFileSync(existsSync(sdkAdb) ? sdkAdb : "adb", args, { encoding: "utf8" }).trim();

const pid = adb("shell", "pidof", APP);
if (!pid) throw new Error("Bar Tracker isn't running on the phone. Open it first.");
adb("forward", `tcp:${PORT}`, `localabstract:webview_devtools_remote_${pid}`);

const pages = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
const ws = new WebSocket(pages[0].webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
ws.send(JSON.stringify({
  id: 1, method: "Runtime.evaluate",
  params: { expression: "window.barDebug.recording()", returnByValue: true },
}));
const rec = await new Promise((resolve, reject) => ws.addEventListener("message", (m) => {
  const d = JSON.parse(m.data);
  if (d.id !== 1) return;
  ws.close();
  if (d.result.exceptionDetails) reject(new Error(d.result.exceptionDetails.exception?.description));
  else resolve(d.result.result.value);
}));
adb("forward", "--remove", `tcp:${PORT}`);

const rows = rec.csv.split("\n").length - 2;
if (rows < 10) throw new Error("No recording in the app yet. Record a set and press Stop.");
const out = join(dirname(fileURLToPath(import.meta.url)), "..", "analysis", "data", rec.name);
writeFileSync(out, rec.csv);
console.log(`Saved ${rows} samples to ${out}`);
