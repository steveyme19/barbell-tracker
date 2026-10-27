// Session export / import format (roadmap 5.1, analytics.md section 7, D30). Pure functions, no
// IndexedDB: store.js reads and writes the records, this turns them into a JSON file and back.
//
//   { format: "bartracker-sessions", version: 1, exported: ISO time,
//     sessions: [{ record: <the session record store.js keeps>,
//                  sets: [{ index, n, cols: { t: { type: "f64", b64 }, ax: { type: "f32", b64 }, ... },
//                           tAbs: { type: "f64", b64 } }] }] }
//
// Raw columns are the stored typed arrays as base64 of their little-endian bytes: exact (a round
// trip gives the same bits) and about half the size of decimal JSON. analysis/sessions.py reads it.

export const FORMAT = "bartracker-sessions";
export const VERSION = 1;
const TYPES = { f64: Float64Array, f32: Float32Array };

function b64(typed) {
  const u8 = new Uint8Array(typed.buffer, typed.byteOffset, typed.byteLength);
  let s = "";
  for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
  return btoa(s);
}

function unb64(text, type) {
  const raw = atob(text);
  const u8 = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) u8[i] = raw.charCodeAt(i);
  return new TYPES[type](u8.buffer);
}

const col = (typed) => ({ type: typed instanceof Float64Array ? "f64" : "f32", b64: b64(typed) });

/** One stored set (store.js's row: { session, index, tAbs, t, ax, ... }) -> its export entry. */
export function setEntry(row, cols) {
  return { index: row.index, n: row.t.length, cols: Object.fromEntries(cols.map((c) => [c, col(row[c])])), tAbs: col(row.tAbs) };
}

/** [{ record, rows }] -> the export object. */
export function toExport(sessions, cols, now = new Date()) {
  return { format: FORMAT, version: VERSION, exported: now.toISOString(),
    sessions: sessions.map(({ record, rows }) => ({ record, sets: rows.map((r) => setEntry(r, cols)) })) };
}

/** The export object (parsed JSON) -> [{ record, rows }] with typed arrays, or throws why not. */
export function fromExport(obj, cols) {
  if (obj?.format !== FORMAT) throw new Error("Not a Bar Tracker session export.");
  if (obj.version !== VERSION) throw new Error(`Export version ${obj.version}: this app reads version ${VERSION}.`);
  return obj.sessions.map(({ record, sets }) => {
    if (!record?.id || !Array.isArray(record.sets)) throw new Error("A session in the export has no id or set list.");
    const rows = sets.map((s) => {
      const row = { session: record.id, index: s.index, tAbs: unb64(s.tAbs.b64, s.tAbs.type) };
      for (const c of cols) {
        if (!s.cols[c]) throw new Error(`Set ${s.index} of ${record.id} has no ${c} column.`);
        row[c] = unb64(s.cols[c].b64, s.cols[c].type);
        if (row[c].length !== s.n) throw new Error(`Set ${s.index} of ${record.id}: ${c} has ${row[c].length} samples, not ${s.n}.`);
      }
      if (row.tAbs.length !== s.n) throw new Error(`Set ${s.index} of ${record.id}: tAbs has ${row.tAbs.length} samples, not ${s.n}.`);
      return row;
    });
    return { record, rows };
  });
}
