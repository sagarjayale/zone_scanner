// Settings page: the lock trailing stop worked through on one simple trade (buy 100, stop 95, target 110), from the
// values in the form, so the numbers can be checked before saving. Pure functions: tested in node
// (tests/test_z_trail_example.py).

// The defaults of the app (app/config.py). "Use the defaults" puts these in the form and saves them.
const TRAIL_DEFAULTS = { trail_method: "lock", trail_tf: "live", trail_lock_pct: 10, trail_lock_gap_pct: 20, trail_lock_behind: false, trail_lock_target: "move" };

const ENTRY = 100, STOP = 95, TARGET = 110, RISK = ENTRY - STOP;
const n2 = (x) => Number(x.toFixed(2));
const rText = (exit) => { const r = n2((exit - ENTRY) / RISK); return `${r > 0 ? "+" : ""}${r} R`; };

// o: { pct, gapPct, behind, live, move }. Returns the step, the gap and two short stories as table rows
// [price, stop, note]: the price falls back after step 1, and the price keeps going up.
function lockExample(o) {
  const pct = Number(o.pct) > 0 ? Number(o.pct) : 10, gapPct = Math.max(0, Number(o.gapPct) || 0);
  const step = n2((TARGET - ENTRY) * pct / 100), gap = n2(step * gapPct / 100);
  const toTarget = Math.max(1, Math.floor((TARGET - ENTRY) / step + 1e-9));
  const when = o.live ? "locked at once (live)" : "locks when the candle closes";
  const level = (k) => n2(ENTRY + k * step);                     // step k
  const held = (k) => (o.behind ? (k <= 1 ? null : level(k - 1)) : level(k));   // the stop once step k is passed
  const stopText = (k) => (held(k) == null ? `${ENTRY} + charges (break-even)` : String(held(k)));
  const exitAt = (k) => (held(k) == null ? ENTRY : held(k));
  const trigger = (k) => n2(ENTRY + k * step + gap);
  const fallBack = [
    [String(n2(trigger(1) - Math.min(0.2, step * 0.2) || level(1))), String(STOP), `not past ${trigger(1)} yet`],
    [String(trigger(1)), stopText(1) + " 🔒", `step 1, ${when}`],
    [String(n2(exitAt(1) - 0.1)), "—", `closed at ${held(1) == null ? "break-even" : held(1)}, ${held(1) == null ? "about 0 R after charges" : rText(exitAt(1))}`],
  ];
  const mid = Math.max(2, Math.floor(toTarget / 2));
  const up = [[String(trigger(2)), stopText(2) + " 🔒", "step 2"]];
  if (mid > 2) up.push([String(trigger(mid)), stopText(mid) + " 🔒", `steps 3 to ${mid} lock together`]);
  if (o.move) {
    up.push([String(TARGET), "", `target moves to ${n2(TARGET + step)}, trade stays open`]);
    const k = toTarget + 1;
    up.push([String(trigger(k)), stopText(k) + " 🔒", `step ${k} (1 beyond the target)`]);
    up.push([String(n2(exitAt(k) - 0.1)), "—", `closed at ${exitAt(k)}, ${rText(exitAt(k))}`]);
  } else {
    up.push([String(TARGET), "—", `closed at the target, ${rText(TARGET)}`]);
  }
  return { entry: ENTRY, stop: STOP, target: TARGET, step, gap, pct, gapPct, fallBack, up };
}

// The example as page elements (needs el() from common.js).
function lockExampleBox(o) {
  const e = lockExample(o);
  const table = (rows, last) => el("table", { class: "board trail-ex" }, el("thead", {}, el("tr", {}, el("th", {}, "Price"), el("th", {}, "Stop"), el("th", {}, last))),
    el("tbody", {}, ...rows.map((r) => el("tr", {}, el("td", { class: "num" }, r[0]), el("td", { class: "num" }, r[1]), el("td", {}, r[2])))));
  return [
    el("p", {}, el("strong", {}, `Example with these settings: buy at ${e.entry}, stop ${e.stop}, target ${e.target}.`)),
    el("p", { class: "sub" }, `Step = ${e.pct}% of ${e.target - e.entry} = ${e.step}. Gap = ${e.gapPct}% of ${e.step} = ${e.gap}.` +
      (o.behind ? " The stop stays one step behind the price." : "")),
    table(e.fallBack, "Note"),
    el("p", { class: "sub" }, "If the price keeps going up instead:"),
    table(e.up, "Note"),
  ];
}

if (typeof module !== "undefined") module.exports = { lockExample, TRAIL_DEFAULTS };
