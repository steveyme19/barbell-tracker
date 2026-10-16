// Tiny canvas line plotter. No libraries.

/**
 * Draw one or more lines on a canvas.
 * series: [{ x: [...], y: [...], color }]
 * spans:  [{ x0, x1, color }]  shaded vertical bands (e.g. reps)
 */
export function drawPlot(canvas, { series, spans = [], yLabel = "", minRange = 1 }) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const css = getComputedStyle(canvas);
  const grid = css.getPropertyValue("--grid").trim() || "#333";
  const text = css.getPropertyValue("--muted").trim() || "#999";

  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const s of series) {
    for (let i = 0; i < s.x.length; i++) {
      if (!Number.isFinite(s.y[i])) continue;
      x0 = Math.min(x0, s.x[i]); x1 = Math.max(x1, s.x[i]);
      y0 = Math.min(y0, s.y[i]); y1 = Math.max(y1, s.y[i]);
    }
  }
  if (!Number.isFinite(x0)) return;
  if (y1 - y0 < minRange) { const c = (y0 + y1) / 2; y0 = c - minRange / 2; y1 = c + minRange / 2; }
  const padY = (y1 - y0) * 0.08; y0 -= padY; y1 += padY;
  if (x1 === x0) x1 = x0 + 1;

  const L = 34, R = 6, T = 6, B = 16;
  const px = (x) => L + ((x - x0) / (x1 - x0)) * (w - L - R);
  const py = (y) => T + (1 - (y - y0) / (y1 - y0)) * (h - T - B);

  for (const s of spans) {
    ctx.fillStyle = s.color;
    ctx.fillRect(px(s.x0), T, px(s.x1) - px(s.x0), h - T - B);
  }

  ctx.strokeStyle = grid;
  ctx.lineWidth = 1;
  ctx.fillStyle = text;
  ctx.font = "10px system-ui, sans-serif";
  const ticks = niceTicks(y0, y1, 4);
  for (const v of ticks) {
    ctx.beginPath(); ctx.moveTo(L, py(v)); ctx.lineTo(w - R, py(v)); ctx.stroke();
    ctx.fillText(String(+v.toFixed(2)), 2, py(v) + 3);
  }
  ctx.fillText(yLabel, L + 4, T + 10);

  for (const s of series) {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    let started = false;
    const step = Math.max(1, Math.floor(s.x.length / (w * 2)));   // skip points we can't see
    for (let i = 0; i < s.x.length; i += step) {
      if (!Number.isFinite(s.y[i])) { started = false; continue; }
      if (!started) { ctx.moveTo(px(s.x[i]), py(s.y[i])); started = true; }
      else ctx.lineTo(px(s.x[i]), py(s.y[i]));
    }
    ctx.stroke();
  }
}

function niceTicks(lo, hi, n) {
  const raw = (hi - lo) / n;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
  return out;
}
