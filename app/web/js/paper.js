// Paper trading page. Positions come from the database; prices and fills arrive over /ws/live.
const $ = (id) => document.getElementById(id);
let data = { pending: [], open: [], closed: [] }, summary = null, prices = {};
const stamp = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "");
const sideTag = (t) => el("span", { class: "tag " + t.side }, t.kind === "option" ? "Option" : t.side === "demand" ? "Long" : "Short");
function say(text, isError = false) { $("status").textContent = text; $("status").className = "status" + (isError ? " err" : ""); }
// The price used on the page: the live price, else the last stored close (market closed, or no live price yet).
const priceOf = (x) => prices[x.instrument_key] ?? x.price ?? x.last_close ?? null;
const fromClose = (x) => prices[x.instrument_key] == null && x.price == null && x.last_close != null;
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
function closeLabel(x) {
  const d = new Date(String(x.last_close_ts).replace(" ", "T"));
  const daily = !isNaN(d) && d.getHours() === 0 && d.getMinutes() === 0;      // a daily close: the day only
  const when = isNaN(d) ? stamp(x.last_close_ts) : `${DAYS[d.getDay()]} ${d.getDate()} ${d.toLocaleString("en", { month: "short" })}` +
    (daily ? "" : ` ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`);
  return `last close ${when}` + (data.market_open === false ? " · market closed" : " · no live price yet");
}
const livePnl = (t) => { const p = priceOf(t); return p == null ? null : (p - t.fill_price) * (t.side === "demand" ? 1 : -1) * t.quantity; };

function renderFigures() {
  const s = summary, fig = $("figures");
  fig.replaceChildren();
  const open = data.open.reduce((sum, t) => sum + (livePnl(t) ?? 0), 0);
  const rows = [["Balance", money(s.balance)], ["Closed profit or loss", signed(s.realised)], [data.open.some(fromClose) ? "Open profit or loss (at the last close)" : "Open profit or loss", signed(open)],
    ["Closed trades", s.closed_trades], ["Win rate", s.win_rate == null ? "none yet" : `${s.win_rate}%`], ["Expectancy per trade", s.expectancy_r == null ? "none yet" : `${s.expectancy_r} R`],
    ["Risk per trade", `${s.risk_pct}% of capital`]];
  for (const [k, v] of rows) fig.append(el("div", {}, el("dt", {}, k), el("dd", {}, String(v))));
}

const history = {};                       // live prices of the last 5 minutes per instrument, for the speed (page memory only)
const PICK_KEY = "zoneScanner.paperNear";
let pick = (() => { try { return localStorage.getItem(PICK_KEY) || "all"; } catch (e) { return "all"; } })();

function ladderBar(L) {
  const bar = el("div", { class: "ladder", role: "img", "aria-label": `Steps from the entry ${L.entry} to the target ${L.target}` });
  bar.append(el("div", { class: "ladder-fill", style: `width:${(L.filled * 100).toFixed(1)}%` }));
  for (const tk of L.ticks) bar.append(el("span", { class: "ladder-tick " + tk.kind, style: `left:${(tk.x * 100).toFixed(1)}%` }));
  bar.append(el("span", { class: "ladder-tick target", style: "left:100%" }));
  if (L.dot != null) bar.append(el("span", { class: "ladder-dot" + (L.losing ? " losing" : ""), style: `left:${(L.dot * 100).toFixed(1)}%` }));
  return el("div", { class: "ladder-wrap" }, bar, el("div", { class: "ladder-ends" }, el("span", {}, L.entry), el("span", {}, `🎯 ${L.target}`)));
}

// "Entry type 1, signal 03 Nov 10:45" under the entry time.
const entryLine = (t) => [ENTRY_TEXT[t.entry_type] || "", t.signal_ts ? `signal ${stamp(t.signal_ts)}` : ""].filter(Boolean).join(", ");

function renderOpen() {
  const open = $("open"); open.replaceChildren();
  if (!data.open.length) open.append(el("tr", {}, el("td", { colspan: "13" }, "No open positions. An entry type 1 order becomes a position when the live price reaches its entry; an entry type 2 or 3 order when its candles have closed.")));
  for (const t of data.open) {
    const p = priceOf(t), pnl = livePnl(t), zone = t.kind !== "option", old = fromClose(t);
    const stop = zone ? el("input", { class: "edit", type: "number", step: String(t.tick || 0.05), value: tickPrice(t.stop, t.tick), "aria-label": "Stop" }) : "";
    const target = zone ? el("input", { class: "edit", type: "number", step: String(t.tick || 0.05), value: tickPrice(t.target, t.tick), "aria-label": "Target" }) : "";
    const tv = zone ? trailLines(t, p, t.tick) : null;
    const trailCell = !tv ? el("td", {}, "") : el("td", { class: "trail-cell" },
      ...tv.gold.map((g) => el("span", { class: "trail-gold" }, g)),
      el("span", { class: "trail-head" }, tv.lines[0]),
      ...(tv.ladder ? [ladderBar(tv.ladder)] : []),
      ...tv.lines.slice(1).map((x) => el("span", { class: "sub wrap" }, x)),
      (!t.trail_view || !t.trail_view.method || t.trail_view.method === "off")
        ? el("button", { class: "btn trail-apply", onclick: () => act(`Trailing applied to ${t.symbol}.`, () => post(`/api/paper/positions/${t.id}/trail`)) }, "Apply trailing from Settings")
        : "");
    const cls = [tv && tv.locked ? "pos-locked" : "", tv && tv.nearStop ? "near-stop" : ""].filter(Boolean).join(" ");
    open.append(el("tr", { class: cls }, el("td", {}, t.symbol, tv && tv.nearStop ? el("span", { class: "near-stop-label" }, "near the stop") : ""),
      el("td", {}, zone ? tfText(t.mode, t.timeframe) : ""),
      el("td", {}, sideTag(t)), el("td", { class: "num" }, String(t.quantity)),
      el("td", { class: "when" }, stamp(t.fill_ts), zone ? el("span", { class: "sub" }, entryLine(t)) : ""),
      el("td", { class: "num" }, fmtPrice(tickPrice(t.fill_price, t.tick))),
      el("td", {}, durationText(t.open_s), t.waited_s >= 60 ? el("span", { class: "sub" }, `waited ${durationText(t.waited_s)} for the fill`) : ""),
      el("td", {}, stop), el("td", {}, target),
      trailCell,
      el("td", { class: "num" }, p == null ? "no price yet" : fmtPrice(tickPrice(p, t.tick)),
        old ? el("span", { class: "sub" }, closeLabel(t)) : data.market_open === false ? el("span", { class: "sub" }, "market closed") : ""),
      el("td", { class: "num " + (pnl > 0 ? "win" : pnl < 0 ? "loss" : "") }, pnl == null ? "" : signed(pnl), pnl != null && old ? el("span", { class: "sub" }, "at the last close") : ""),
      el("td", {}, el("div", { class: "rowbtns" },
        el("button", { class: "btn", onclick: () => showView({ trade_id: t.id }) }, t.kind === "option" ? "Details" : "Chart"),
        zone ? el("button", { class: "btn", onclick: () => act(`Stop and target saved for ${t.symbol}.`, () => api(`/api/paper/positions/${t.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ stop: Number(stop.value), target: Number(target.value) }) })) }, "Save") : "",
        el("button", { class: "btn", onclick: () => act(`${t.symbol} closed.`, () => post(`/api/paper/positions/${t.id}/close`)) }, "Close now")))));
  }
}

function renderPending() {
  const now = Date.now(), closedMarket = data.market_open === false;
  const rows = data.pending.map((o) => {
    const p = priceOf(o), old = fromClose(o);
    const d = entryDistance(o, p);
    const sp = old ? { kind: "closed", text: closeLabel(o) } : closedMarket ? { kind: "closed", text: "market closed" } : entrySpeed(o, history[o.instrument_key], now);
    return { o, p, d, sp, old };
  }).sort((a, b) => (a.d ? a.d.r : 1e9) - (b.d ? b.d.r : 1e9));
  // chips with live counts
  const counts = { all: rows.length };
  for (const g of NEAR_GROUPS) counts[g.key] = rows.filter((x) => x.d && x.d.group === g.key).length;
  const chips = $("near-chips"); chips.replaceChildren();
  for (const [key, text] of [...NEAR_GROUPS.map((g) => [g.key, `${g.icon} ${g.label}`]), ["all", "All"]]) {
    chips.append(el("button", { class: "chip near-" + key, "aria-pressed": String(pick === key),
      onclick: () => { pick = key; try { localStorage.setItem(PICK_KEY, key); } catch (e) { /* storage not available */ } renderPending(); } }, `${text} ${counts[key]}`));
  }
  const pend = $("pending"); pend.replaceChildren();
  const shownRows = rows.filter((x) => pick === "all" || (x.d && x.d.group === pick));
  if (!data.pending.length) pend.append(el("tr", {}, el("td", { colspan: "13" }, "No pending orders. Use Paper trade on a setup in the scanner, or switch on automatic orders above.")));
  else if (!shownRows.length) pend.append(el("tr", {}, el("td", { colspan: "13" }, "No pending order in this group right now. Press All to see every order.")));
  const delay = `${-(now % 1600)}ms`;          // keeps the pulse smooth across re-renders
  for (const { o, p, d, sp, old } of shownRows) {
    const g = d ? NEAR_GROUPS.find((x) => x.key === d.group) : null;
    const dist = !d ? el("td", { class: "near" }, el("span", { class: "sub" }, "no live price yet"), o.note ? el("span", { class: "order-note" }, o.note) : "")
      : el("td", { class: "near" }, el("span", { class: "near-badge near-" + d.group, style: d.group === "now" ? `animation-delay:${delay}` : "" }, `${g.icon} ${g.label.toUpperCase()}`),
        el("span", { class: "near-line" }, d.at ? "price is at the entry" : `${d.r} R · ${d.pct}% away`),
        el("span", { class: "near-bar" }, el("span", { style: `width:${(d.fill * 100).toFixed(0)}%` })),
        el("span", { class: "sub" }, sp.text),
        o.wait ? el("span", { class: "sub wrap" }, o.wait) : "",
        o.note ? el("span", { class: "order-note" }, o.note) : "");
    pend.append(el("tr", { class: d ? "near-row near-" + d.group : "" }, el("td", {}, o.symbol), dist,
      el("td", {}, tfText(o.mode, o.timeframe)), el("td", {}, sideTag(o)), el("td", { class: "num" }, String(o.quantity)),
      el("td", {}, ENTRY_TEXT[o.entry_type] || "Entry type 1", el("span", { class: "sub wrap" }, o.entry_type === "TYPE_2" || o.entry_type === "TYPE_3" ? `waits until ${ENTRY_HOW[o.entry_type]}` : "limit at the proximal line")),
      el("td", { class: "num" }, fmtPrice(tickPrice(o.entry, o.tick)), el("span", { class: "sub" }, o.entry_type === "TYPE_2" || o.entry_type === "TYPE_3" ? "proximal line, until the entry" : "")),
      el("td", { class: "num" }, fmtPrice(tickPrice(o.stop, o.tick))), el("td", { class: "num" }, fmtPrice(tickPrice(o.target, o.tick))),
      el("td", { class: "num" }, p == null ? "" : fmtPrice(tickPrice(p, o.tick)), old ? el("span", { class: "sub" }, "last close") : closedMarket ? el("span", { class: "sub" }, "market closed") : ""),
      el("td", { class: "when" }, stamp(o.created_at), o.created_at && data.now ? el("span", { class: "sub" }, `${durationText((new Date(String(data.now).replace(" ", "T")) - new Date(String(o.created_at).replace(" ", "T"))) / 1000)} ago`) : ""),
      el("td", { class: "when" }, stamp(o.signal_ts), el("span", { class: "sub" }, MODE_LABEL[o.mode] || o.mode || "")),
      el("td", {}, el("div", { class: "rowbtns" },
        el("button", { class: "btn", onclick: () => showView({ order_id: o.id }) }, "Chart"),
        el("button", { class: "btn primary", onclick: () => enterNow(o) }, "Enter now at market"),
        el("button", { class: "btn", onclick: () => act(`Order for ${o.symbol} cancelled.`, () => api(`/api/paper/orders/${o.id}`, { method: "DELETE" })) }, "Cancel order")))));
  }
}

function render() {
  renderFigures();
  // While a stop or target box is being edited, the open positions table is left alone (a price tick would reset it).
  const editing = document.activeElement && document.activeElement.closest && document.activeElement.closest("#open");
  if (!editing) renderOpen();
  renderPending();
  const closed = $("closed"); closed.replaceChildren();
  if (!data.closed.length) closed.append(el("tr", {}, el("td", { colspan: "14" }, "No closed trades yet.")));
  const why = { stop: "Stop", trail: "Trailing stop", target: "Target", manual: "Closed by you" };
  for (const t of data.closed) {
    closed.append(el("tr", {}, el("td", {}, t.symbol), el("td", {}, t.kind === "option" ? "" : tfText(t.mode, t.timeframe)), el("td", {}, sideTag(t)),
      el("td", { class: "num" }, String(t.quantity)),
      el("td", { class: "when" }, stamp(t.fill_ts), t.kind === "option" ? "" : el("span", { class: "sub" }, entryLine(t))), el("td", { class: "when" }, stamp(t.exit_ts)),
      el("td", {}, durationText(t.open_s), t.waited_s >= 60 ? el("span", { class: "sub" }, `waited ${durationText(t.waited_s)} for the fill`) : ""),
      el("td", { class: "num" }, fmtPrice(tickPrice(t.fill_price, t.tick))), el("td", { class: "num" }, fmtPrice(tickPrice(t.exit_price, t.tick))), el("td", {}, why[t.exit_reason] || ""),
      el("td", { class: "num" }, t.charges == null ? "" : fmtPrice(t.charges)),
      el("td", { class: "num " + (t.result === "win" ? "win" : "loss") }, signed(t.pnl),
        t.pnl_after_charges == null ? "" : el("span", { class: "sub" }, `after charges ${signed(t.pnl_after_charges)}`)), el("td", { class: "num" }, t.result_r == null ? "" : `${t.result_r > 0 ? "+" : ""}${t.result_r} R`),
      el("td", {}, el("button", { class: "btn", onclick: () => showView({ trade_id: t.id }) }, t.kind === "option" ? "Details" : "Chart"))));
  }
}

// "Enter now at market": fill a pending order at once at the live price (plus slippage), whatever its entry type.
async function enterNow(o) {
  const p = priceOf(o);
  const text = `Enter ${o.symbol} now at the market price${p == null ? "" : ` (about ${fmtPrice(p)})`} instead of waiting for ${o.entry_type === "TYPE_2" || o.entry_type === "TYPE_3" ? "its candles" : `the entry at ${fmtPrice(o.entry)}`}?\n\nThe stop loss stays at ${fmtPrice(o.stop)}. The target and quantity are worked out again from the real entry. This is paper trading: no order is sent to your broker.`;
  if (!confirm(text)) return;
  await act(`${o.symbol} entered at market.`, () => post(`/api/paper/orders/${o.id}/enter`));
}

// ---- chart view of one order or trade -------------------------------------------
let viewing = null;
function kvList(pairs) { const dl = el("dl", { class: "kv" }); for (const [k, v] of pairs) if (v !== null && v !== undefined && v !== "") dl.append(el("dt", {}, k), el("dd", {}, String(v))); return dl; }
// The trailing stop a position was filled with, and where it stands.
function trailText(st) {
  if (!st || !st.trail_method || st.trail_method === "off") return "off (the stop stays where it was set)";
  const how = st.trail_method === "lock" ? `lock, steps of ${st.trail_lock_pct}% of the target` : `${st.trail_method}, from +${st.trail_start_r} R`;
  const now = st.moves ? `moved ${st.moves}×, best +${st.best_r} R` : st.active ? "started, not moved yet" : "waiting to start";
  return `${how} on ${TF_LABEL[st.tf] || st.tf} candles: ${now}${st.no_target ? ", target removed" : ""}`;
}
async function showView(ref, scroll = true) {
  viewing = ref;
  const box = $("view");
  box.hidden = false;
  if (scroll) $("view-detail").replaceChildren(el("p", { class: "empty" }, "Loading"));
  if (scroll) box.scrollIntoView({ behavior: "smooth", block: "start" });
  let d;
  try { d = await api(`/api/paper/detail?${ref.order_id ? "order_id=" + ref.order_id : "trade_id=" + ref.trade_id}`); }
  catch (e) { $("view-detail").replaceChildren(el("p", { class: "empty" }, `Could not load it. ${e.message}`)); return; }
  const o = d.order, t = d.trade, z = d.zone, p = d.plan, side = (t || o).side, buy = side === "demand";
  const status = t ? (t.exit_ts ? `closed: ${({ stop: "stop loss", trail: "trailing stop", target: "target", manual: "closed by you" })[t.exit_reason] || t.exit_reason}` : "open position") : `pending order (${o.status})`;
  $("view-title").textContent = `${d.symbol}: ${d.kind === "option" ? "option" : buy ? "long" : "short"}, ${status}`;
  const actions = $("view-actions"); actions.replaceChildren();
  // "Open full chart": the Chart page on this stock, in the plan's mode and on the zone's timeframe, in a new tab
  if (d.kind !== "option") {
    const q = new URLSearchParams({ symbol: d.instrument_key });
    if (p && p.mode) q.set("mode", p.mode);
    if ((t || o).timeframe) q.set("tf", (t || o).timeframe);
    actions.append(el("a", { class: "btn", href: "/chart?" + q.toString(), target: "_blank", rel: "noopener" }, "Open full chart"));
  }
  if (o && o.status === "pending") actions.append(el("button", { class: "btn primary", onclick: () => enterNow({ ...o, symbol: d.symbol, price: d.price }) }, "Enter now at market"));
  const r = (v) => (v == null ? null : `${v > 0 ? "+" : ""}${v} R`);
  const rows = [["Status", status], ["Live price", d.price == null ? "none" : fmtPrice(d.price)]];
  if (p) rows.push(["Mode", modeText(p.mode)], ["Entry type", ENTRY_TEXT[p.entry_type] || p.entry_type],
    ["Zone used", p.is_default === false ? "another LTF zone (not the mode's default zone)" : "the mode's default LTF zone"],
    ["Trade score", p.score == null ? null : `${p.score} (${LABEL_TEXT[p.label] || p.label})`], ["Curve when planned", lower(p.curve)],
    ["ITF trend when planned", lower(p.itf_trend)], ["Plan", `version ${p.version}, ${p.status}`]);
  if (z) rows.push(["Zone", `${TF_LABEL[z.timeframe] || z.timeframe} ${z.side} ${z.pattern}: ${fmtPrice(z.proximal)} to ${fmtPrice(z.distal)}`],
    ["Zone formed", String(z.created_ts).replace("T", " ").slice(0, 16)], ["Zone now", `${z.status}, ${z.touches} ${z.touches === 1 ? "test" : "tests"}`]);
  if (o) rows.push(["Order placed", String(o.created_at || "").replace("T", " ").slice(0, 16)], ["Order note", o.note]);
  const lead = t || o;
  rows.push(["Entry", fmtPrice(t ? t.fill_price : o.entry)], ["Stop loss", fmtPrice(lead.stop)], ["Target", fmtPrice(lead.target)], ["Quantity", lead.quantity]);
  if (t) rows.push(["Filled", String(t.fill_ts).replace("T", " ").slice(0, 16)], ["Exited", t.exit_ts ? `${String(t.exit_ts).replace("T", " ").slice(0, 16)} at ${fmtPrice(t.exit_price)}` : null],
    ["Profit or loss", t.exit_ts ? signed(t.pnl) : t.open_pnl == null ? null : `${signed(t.open_pnl)} (open)`], ["Result", r(t.result_r)],
    ["Trailing stop", trailText(t.trail)],
    ["Changes", (t.history || []).map((h) => `${String(h.time).replace("T", " ").slice(0, 16)} ${h.field} ${fmtPrice(h.old)} to ${h.new == null ? "none" : fmtPrice(h.new)}${h.note ? ` (${h.note})` : ""}`).join("; ")]);
  $("view-detail").replaceChildren(kvList(rows));
  const chartBox = $("view-chart");
  chartBox.hidden = d.kind === "option" || !(t || o).timeframe;
  if (chartBox.hidden) { $("view-detail").append(el("p", { class: "empty" }, "No chart for option positions: the app does not store option candles.")); return; }
  try {
    await TradeChart.show(chartBox, { instrument_key: d.instrument_key, timeframe: (t || o).timeframe, side,
      zone_created_ts: z ? (z.base_start_ts || z.created_ts) : null, zone_proximal: z ? z.proximal : null, zone_distal: z ? z.distal : null,
      entry_ts: t ? t.fill_ts : null, exit_ts: t ? t.exit_ts : null, entry: t ? t.fill_price : o.entry, stop: lead.stop, target: lead.target,
      result: t ? (t.exit_ts ? t.result : "open") : "pending" }, null);
  } catch (e) { $("view-detail").append(el("p", { class: "empty" }, `Could not load the candles. ${e.message}`)); }
}
$("view-close").onclick = () => { viewing = null; $("view").hidden = true; };

async function load() {
  [data, summary] = await Promise.all([api("/api/paper/positions"), api("/api/paper/summary")]);
  $("auto").checked = summary.auto; $("auto-score").value = String(summary.auto_min_score); $("auto-closed").checked = Boolean(summary.auto_closed);
  render();
  if (viewing) showView(viewing, false);
}
async function act(done, fn) { try { await fn(); say(done); await load(); } catch (e) { say(e.message, true); } }

async function saveAuto() {
  await act($("auto").checked ? `Automatic orders on for score ${$("auto-score").value} and above.` : "Automatic orders off.",
    () => api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paper_auto: $("auto").checked, paper_auto_min_score: Number($("auto-score").value) }) }));
}
$("auto").onchange = saveAuto; $("auto-score").onchange = saveAuto;
$("auto-closed").onchange = () => act($("auto-closed").checked ? "Automatic orders will also be placed after a scan while the market is closed." : "Automatic orders only in market hours.",
  () => api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paper_auto_closed: $("auto-closed").checked }) }));
// "Place automatic orders now": from the last saved scan. Shows what was placed and why the others were skipped.
$("auto-run").onclick = async () => {
  const btn = $("auto-run"); btn.disabled = true;
  try {
    const r = await post("/api/paper/auto/run");
    const box = $("auto-result"); box.hidden = false;
    const placed = r.placed.map((o) => `${o.symbol} ${o.side === "demand" ? "buy" : "sell"} ${fmtPrice(o.entry)}`);
    box.replaceChildren(
      el("p", { class: "auto-head" }, `${r.placed.length} ${r.placed.length === 1 ? "order" : "orders"} placed${placed.length ? ": " + placed.join(", ") : ""}` +
        (r.market_open ? "" : " (market closed: they fill from the open)")),
      ...(r.skipped.length ? [el("p", { class: "sub" }, `${r.skipped.length} skipped:`),
        el("ul", { class: "auto-skips" }, ...r.skipped.slice(0, 30).map((x) => el("li", {}, `${x.symbol}: ${x.why}`)))] : []),
      el("button", { class: "btn", onclick: () => { box.hidden = true; } }, "Hide"));
    await load();
  } catch (e) { say(e.message, true); } finally { btn.disabled = false; }
};
$("reset").onclick = () => { if (confirm("Archive every paper order and trade and start again from the starting capital?")) act("Paper account reset.", () => post("/api/paper/reset")); };

let reloadTimer = null;
const reloadSoon = () => { clearTimeout(reloadTimer); reloadTimer = setTimeout(() => load().catch(() => {}), 1000); };   // many events: one reload
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`);
  ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.type === "prices") { Object.assign(prices, m.data); const t = Date.now(); for (const o of data.pending) trackPrice(history, o.instrument_key, prices[o.instrument_key], t); if (summary) render(); } else if (m.type === "paper" || m.type === "scan") reloadSoon(); };
  ws.onclose = () => setTimeout(connect, 3000);
}
setInterval(() => { if (!document.hidden) load().catch(() => {}); }, 30000);    // market open or closed, and anything the price feed missed
showToken($("token"));
load().catch((e) => say(`Could not load paper trading. ${e.message}`, true));
connect();
