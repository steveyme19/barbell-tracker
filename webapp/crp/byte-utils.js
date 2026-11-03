/*
 * Browser byte utilities — replaces Node Buffer for CRP protocol handling.
 * All functions work with Uint8Array.
 */

const encoder = new TextEncoder();
const decoder = new TextDecoder();

/** Allocate a zero-filled Uint8Array of length n. */
export function allocBytes(n) {
  return new Uint8Array(n);
}

/** Concatenate an array of Uint8Arrays into a single Uint8Array. */
export function concatBytes(arrays) {
  let total = 0;
  for (const a of arrays) total += a.length;
  const out = new Uint8Array(total);
  let off = 0;
  for (const a of arrays) {
    out.set(a, off);
    off += a.length;
  }
  return out;
}

/** Copy src into dest at the given offset. */
export function copyBytes(src, dest, offset) {
  dest.set(src, offset);
}

/** Encode a string to UTF-8 Uint8Array. */
export function fromString(str) {
  return encoder.encode(str);
}

/** Decode a Uint8Array (or subarray) to a UTF-8 string. */
export function toString(buf) {
  return decoder.decode(buf);
}

/** Convert a Uint8Array to a hex string. */
export function toHex(buf) {
  let hex = '';
  for (let i = 0; i < buf.length; i++) {
    hex += buf[i].toString(16).padStart(2, '0');
  }
  return hex;
}
