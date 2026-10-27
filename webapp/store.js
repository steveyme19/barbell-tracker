// Session history in the browser's IndexedDB (roadmap 5.1, analytics.md section 7).
// Two stores:
//   sessions: one small record per workout (the set list without raw data), for the
//             history list and for resuming a session the app lost (killed, crashed).
//   sets:     each set's raw samples as typed-array columns, so a past set can be
//             re-analyzed or exported again. Same samples the CSV files hold.
// Browser storage can be wiped (cleared site data, low space), so we ask for persistent
// storage, and every set is also in Documents/BarTracker in the app (the real backup).

const DB_NAME = "bartracker";
const COLS = ["t", "ax", "ay", "az", "gx", "gy", "gz"];
let dbp = null;

function open() {
  dbp ??= new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      db.createObjectStore("sessions", { keyPath: "id" });
      db.createObjectStore("sets", { keyPath: ["session", "index"] });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbp;
}

// One request in one transaction, as a promise.
async function run(store, mode, fn) {
  const db = await open();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, mode);
    const req = fn(tx.objectStore(store));
    tx.oncomplete = () => resolve(req?.result);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

/** Ask the browser not to evict our data under storage pressure. Fine if it says no. */
export async function askPersist() {
  try { return await navigator.storage?.persist?.(); } catch { return false; }
}

/** Save (or update) a session record: { id, ...summary fields, sets: [set summaries] }. */
export function putSession(rec) {
  return run("sessions", "readwrite", (s) => s.put(rec));
}

export function getSession(id) {
  return run("sessions", "readonly", (s) => s.get(id));
}

/** All session records, newest first. */
export async function listSessions() {
  const all = (await run("sessions", "readonly", (s) => s.getAll())) ?? [];
  return all.sort((a, b) => b.startedAtMs - a.startedAtMs);
}

/** A set's raw samples: [{ t, ax, ... }], with tAbs = its session times. */
export function putSetSamples(sessionId, index, samples, tAbs) {
  const rec = { session: sessionId, index, tAbs: Float64Array.from(tAbs) };
  for (const c of COLS) {
    // time needs full precision; the rest are sensor readings (float32 is what they are)
    rec[c] = c === "t" ? Float64Array.from(samples, (x) => x.t) : Float32Array.from(samples, (x) => x[c] ?? 0);
  }
  return run("sets", "readwrite", (s) => s.put(rec));
}

export async function getSetSamples(sessionId, index) {
  const rec = await run("sets", "readonly", (s) => s.get([sessionId, index]));
  if (!rec) return null;
  const samples = Array.from(rec.t, (_, i) => Object.fromEntries(COLS.map((c) => [c, rec[c][i]])));
  return { samples, tAbs: Array.from(rec.tAbs) };
}

// ---------- export, import, delete (roadmap 5.1; the file format is export.js) ----------

const sessionRange = (id) => IDBKeyRange.bound([id, -Infinity], [id, Infinity]);

/** The session record and every stored set row of it, as typed arrays: what export.js writes. */
export async function getSessionData(id) {
  const record = await getSession(id);
  if (!record) return null;
  const rows = (await run("sets", "readonly", (s) => s.getAll(sessionRange(id)))) ?? [];
  return { record, rows };
}

/** A session and its raw sets out of IndexedDB, in one transaction (both or neither). The CSV
 * files the app saved in Documents/BarTracker stay: they're the real backup. */
export async function deleteSession(id) {
  const db = await open();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(["sessions", "sets"], "readwrite");
    tx.objectStore("sessions").delete(id);
    tx.objectStore("sets").delete(sessionRange(id));
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

/** Store imported sessions ([{ record, rows }] from export.js) that aren't here yet; one already
 * stored (same id) is left as it is. Returns { added, skipped } (ids). */
export async function importSessions(list) {
  const db = await open();
  const have = new Set(((await run("sessions", "readonly", (s) => s.getAllKeys())) ?? []));
  const fresh = list.filter(({ record }) => !have.has(record.id));
  await new Promise((resolve, reject) => {
    const tx = db.transaction(["sessions", "sets"], "readwrite");
    for (const { record, rows } of fresh) {
      tx.objectStore("sessions").put(record);
      for (const r of rows) tx.objectStore("sets").put(r);
    }
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
  return { added: fresh.map(({ record }) => record.id), skipped: list.filter(({ record }) => have.has(record.id)).map(({ record }) => record.id) };
}

export { COLS };
