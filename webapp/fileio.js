// Reading and writing recordings. Pure functions (no DOM), so Node can use them too.
//
// Sample format shared by every source (decision D4):
//   { t, ax, ay, az, gx, gy, gz }   t in s, a in m/s^2 (including gravity), g in deg/s

/** Parse a phyphox-style CSV into column arrays. */
export function parseCSV(text) {
  const lines = text.trim().split(/\r?\n/);
  const header = splitRow(lines[0]);
  const find = (name) => header.findIndex((h) => h.includes(name));
  const cols = {
    t: 0,
    ax: find("Acceleration x"),
    ay: find("Acceleration y"),
    az: find("Acceleration z"),
    gx: find("Gyro x"),
    gy: find("Gyro y"),
    gz: find("Gyro z"),
  };
  if (cols.ax < 0 || cols.ay < 0 || cols.az < 0) {
    throw new Error(`Need Acceleration x/y/z columns. Found: ${header.join(", ")}`);
  }
  const out = { t: [], ax: [], ay: [], az: [], gx: [], gy: [], gz: [] };
  for (let i = 1; i < lines.length; i++) {
    const row = splitRow(lines[i]);
    if (row.length < 4) continue;
    for (const key of Object.keys(out)) {
      out[key].push(cols[key] >= 0 ? Number(row[cols[key]]) : 0);
    }
  }
  return out;
}

function splitRow(line) {
  return line.split(",").map((s) => s.trim().replace(/^"|"$/g, ""));
}

/** Turn samples into a CSV that analysis/phone_lift.py and analysis/reps.py can read. */
export function toCSV(samples) {
  const head = [
    "Time (s)", "Acceleration x (m/s^2)", "Acceleration y (m/s^2)", "Acceleration z (m/s^2)",
    "Absolute acceleration (m/s^2)", "Gyro x (deg/s)", "Gyro y (deg/s)", "Gyro z (deg/s)",
  ].map((h) => `"${h}"`).join(",");
  const t0 = samples.length ? samples[0].t : 0;
  const rows = samples.map((s) => {
    const abs = Math.sqrt(s.ax * s.ax + s.ay * s.ay + s.az * s.az);
    return [s.t - t0, s.ax, s.ay, s.az, abs, s.gx, s.gy, s.gz]
      .map((x) => (Number.isFinite(x) ? x.toPrecision(7) : "0")).join(",");
  });
  return [head, ...rows].join("\n") + "\n";
}

/**
 * Pull one file out of a zip (phyphox exports "Raw Data.csv" inside a zip).
 * Minimal reader: stored or deflated entries, which is all phyphox makes.
 */
export async function readZipEntry(buffer, wanted) {
  const dv = new DataView(buffer);
  let eocd = -1;
  for (let i = buffer.byteLength - 22; i >= 0; i--) {
    if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error("Not a zip file.");
  const count = dv.getUint16(eocd + 10, true);
  let p = dv.getUint32(eocd + 16, true);
  const dec = new TextDecoder();
  for (let k = 0; k < count; k++) {
    if (dv.getUint32(p, true) !== 0x02014b50) throw new Error("Bad zip directory.");
    const method = dv.getUint16(p + 10, true);
    const csize = dv.getUint32(p + 20, true);
    const nameLen = dv.getUint16(p + 28, true);
    const extraLen = dv.getUint16(p + 30, true);
    const commentLen = dv.getUint16(p + 32, true);
    const local = dv.getUint32(p + 42, true);
    const name = dec.decode(new Uint8Array(buffer, p + 46, nameLen));
    if (name === wanted) {
      const lNameLen = dv.getUint16(local + 26, true);
      const lExtraLen = dv.getUint16(local + 28, true);
      const start = local + 30 + lNameLen + lExtraLen;
      const data = new Uint8Array(buffer, start, csize);
      if (method === 0) return dec.decode(data);
      if (method !== 8) throw new Error(`Unsupported zip compression (${method}).`);
      const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
      return await new Response(stream).text();
    }
    p += 46 + nameLen + extraLen + commentLen;
  }
  throw new Error(`"${wanted}" not found in zip.`);
}

/** Read a phyphox export (CSV, or the zip with "Raw Data.csv" inside). */
export async function parseRecording(name, buffer) {
  if (name.toLowerCase().endsWith(".zip")) {
    return parseCSV(await readZipEntry(buffer, "Raw Data.csv"));
  }
  if (name.toLowerCase().endsWith(".xlsx")) {
    throw new Error("Excel files aren't supported in the app. Export from phyphox as CSV instead.");
  }
  return parseCSV(new TextDecoder().decode(buffer));
}
