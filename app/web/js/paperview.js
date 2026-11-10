// Paper page: the numbers behind the "Distance to entry" and "Trailing / locked" columns. Pure functions of the
// order or position, the live price and (for the speed) a few minutes of prices kept in page memory only.
// Nothing here talks to the server, so it can be tested on its own (tests/test_z_paper_ladder.py runs it in node).

// Prices rounded to the stock's tick size (NSE equity 0.05 unless the instrument says otherwise).
function tickPrice(p, tick) {
  if (p == null || !isFinite(p)) return null;
  const t = tick && tick > 0 ? tick : 0.05;
  return Math.round(Math.round(p / t) * t * 100) / 100;
}

// ---- pending orders: how near the entry is ----------------------------------
// R = entry to stop. A buy limit waits below the price, a sell limit above it; the distance counts only the way
// the price still has to travel to reach the entry (0 once it is at or past it).
const NEAR_GROUPS = [
  { key: "now", label: "Entering now", icon: "🔴", max: 0.25 },
  { key: "close", label: "Close", icon: "🟢", max: 1 },
  { key: "between", label: "Between", icon: "🟡", max: 3 },
  { key: "far", label: "Far", icon: "⚪", max: Infinity },
];
function entryDistance(o, price) {
  const r = Math.abs(o.entry - o.stop);
  if (price == null || !(r > 0)) return null;
  const toward = o.side === "demand" ? price - o.entry : o.entry - price;      // still to travel toward the entry
  const dist = Math.max(toward, 0);
  const inR = dist / r;
  const group = inR < 0.25 ? NEAR_GROUPS[0] : inR <= 1 ? NEAR_GROUPS[1] : inR <= 3 ? NEAR_GROUPS[2] : NEAR_GROUPS[3];
  return { r: Math.round(inR * 100) / 100, pct: Math.round((dist / price) * 1000) / 10, at: dist === 0, group: group.key,
    fill: Math.max(0, Math.min(1, 1 - inR / 3)) };       // the bar: empty at 3 R or more, full at the entry
}

// Direction and speed toward the entry, from the prices seen in the last 5 minutes (page memory only).
const TRACK_MS = 5 * 60 * 1000, MEASURE_MS = 60 * 1000, FLAT_R = 0.05;
function trackPrice(history, id, price, now) {
  const list = (history[id] = (history[id] || []).filter((p) => now - p.t <= TRACK_MS));
  if (price != null && (!list.length || list[list.length - 1].price !== price || now - list[list.length - 1].t > 15000)) list.push({ t: now, price });
  return list;
}
function entrySpeed(o, list, now) {
  if (!list || !list.length || now - list[0].t < MEASURE_MS) return { kind: "measuring", text: "measuring…" };
  const first = entryDistance(o, list[0].price), last = entryDistance(o, list[list.length - 1].price);
  if (!first || !last) return { kind: "measuring", text: "measuring…" };
  const moved = first.r - last.r;                       // in R, positive = closer
  const closerArrow = o.side === "demand" ? "⬇" : "⬆", awayArrow = o.side === "demand" ? "⬆" : "⬇";
  if (Math.abs(moved) < FLAT_R) return { kind: "flat", text: "➖ flat" };
  if (moved < 0) return { kind: "away", text: `${awayArrow} moving away` };
  const perMin = moved / ((list[list.length - 1].t - list[0].t) / 60000);
  const mins = perMin > 0 ? last.r / perMin : null;
  const when = last.at ? "" : mins == null ? "" : mins < 1 ? ", under a minute at this speed" : `, ~${mins < 90 ? Math.round(mins) + " min" : Math.round(mins / 60) + " hr"} at this speed`;
  return { kind: "closer", text: `${closerArrow} coming closer${when}`, minutes: mins };
}

// ---- open positions: the stop and the lock ladder ---------------------------
// v: the trail_view the server sends; price: the live price. Returns the lines and the ladder bar's parts.
function trailLines(t, price, tick) {
  const v = t.trail_view || {};
  const sign = t.side === "demand" ? 1 : -1, px = (p) => fmtPrice(tickPrice(p, tick));
  const risk = v.risk || Math.abs(t.fill_price - t.stop);
  const lockedR = v.locked_r ?? ((t.stop - t.fill_price) * sign / risk);
  const out = { locked: false, nearStop: false, lines: [], ladder: null, gold: [] };
  if (price != null && risk > 0) out.nearStop = (price - t.stop) * sign <= 0.3 * risk;     // within 0.3 R of the stop
  const keeps = () => `keeps ${lockedR > 0 ? "+" : ""}${lockedR} R, ${signed(v.locked_inr)} INR` +
    (v.locked_after_charges != null ? ` (after charges about ${signed(v.locked_after_charges)})` : "");
  if (v.method !== "lock" || !v.step) {
    if (v.method && v.method !== "off" && v.moves) {
      out.locked = lockedR > 0;
      out.lines.push(`🔒 ${v.method} trailing · stop moved ${v.moves}× · stop ${px(t.stop)}`, keeps());
    } else {
      out.lines.push(`${v.method && v.method !== "off" ? `🔓 ${v.method} trailing, not moved yet` : "No trailing"} · stop ${px(t.stop)}: risk ${lockedR > 0 ? "+" : ""}${lockedR} R`);
    }
    return out;
  }
  const n = v.steps_to_target, k = v.steps, beyond = v.beyond_target || 0;
  out.locked = k > 0;
  out.lines.push(k > 0 ? `🔒 Step ${k}${beyond ? ` (${beyond} beyond target)` : ""} of ${n} locked · stop ${px(t.stop)}`
    : `🔓 Not locked yet · Step 0 of ${n}`);
  out.lines.push(k > 0 ? keeps() : `stop ${px(t.stop)}: risk ${lockedR} R`);
  if (v.target_reached && v.target_mode === "move") out.gold.push("🎯 First target reached · running beyond target");
  const reached = price != null && (price - v.next_trigger) * sign >= 0;
  const away = price != null && !reached ? ` (${Math.round(Math.abs(v.next_trigger - price) / price * 1000) / 10}% away)` : "";
  out.lines.push(`Next: step ${k + 1} → stop ${px(v.next_stop)} when price reaches ${px(v.next_trigger)}${reached ? (v.live ? " (reached: locking now)" : " (reached: locks at the next candle close)") : away}`);
  if (v.behind) out.lines.push("The stop stays one step behind the price (step 1 locks break-even).");
  out.lines.push(v.target_mode === "move" ? "The target moves forward one step each time it is reached; only the locked stop ends the trade."
    : "Target closes the trade.");
  // The ladder: from the entry to the current target (or the highest step, once beyond it)
  const top = Math.max((v.target - t.fill_price) * sign, (v.levels.length ? (v.levels[v.levels.length - 1] - t.fill_price) * sign : 0), v.step);
  const at = (p) => Math.max(0, Math.min(1, ((p - t.fill_price) * sign) / top));
  out.ladder = { ticks: v.levels.map((p, i) => ({ x: at(p), kind: i < k ? "locked" : i === k ? "next" : "step" })),
    filled: at(t.stop), dot: price == null ? null : at(price), losing: price != null && (price - t.fill_price) * sign < 0,
    entry: px(t.fill_price), target: px(v.target) };
  return out;
}

// ---- tables: how long, and which timeframe -----------------------------------
function durationText(seconds) {
  if (seconds == null || !isFinite(seconds)) return "";
  const s = Math.max(0, Math.round(seconds)), d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
}
// "Daily · 30 min zone": the mode of the plan and the timeframe of the zone.
function tfText(mode, tf) {
  const m = mode ? (typeof MODE_LABEL !== "undefined" && MODE_LABEL[mode]) || mode : "";
  const z = tf ? `${(typeof TF_LABEL !== "undefined" && TF_LABEL[tf]) || tf} zone` : "";
  return [m, z].filter(Boolean).join(" · ");
}

if (typeof module !== "undefined") module.exports = { tickPrice, entryDistance, trackPrice, entrySpeed, trailLines, durationText, tfText, NEAR_GROUPS };
