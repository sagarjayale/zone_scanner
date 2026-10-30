// Dashboard: one full GTF result per ready stock for the chosen mode (Monthly by default).
// Stocks appear as soon as their own download and scan are done; the rest carry on in the background.
const $ = (id) => document.getElementById(id);
const state = { board: null, prices: {}, status: null, open: null, reach: 1, pick: "" };
const STATE_TEXT = { TOUCHING_NOW: "Touching now", APPROACHING: "Approaching", JUST_FORMED: "Just formed", ACTIVE: "Active" };
const STATE_CLASS = { TOUCHING_NOW: "in-zone", APPROACHING: "approaching", JUST_FORMED: "fresh", ACTIVE: "fresh" };
const WORK_TEXT = { queued: "waiting to start", downloading: "downloading", scanning: "scanning", failed: "failed", ready: "ready" };
const clock = (s) => (s ? String(s).replace("T", " ").slice(11, 16) : "");

function say(text, isError = false) { $("status").textContent = text; $("status").className = "status" + (isError ? " err" : ""); }
const priceOf = (r) => state.prices[r.instrument_key] ?? r.live_price ?? r.cmp;

// The state from the live price: touching when price is at or inside the zone, approaching within the set percentage.
function liveState(r) {
  const z = r.ltf_zone, live = state.prices[r.instrument_key] ?? r.live_price;
  if (!z || live == null) return { key: r.zone_state, note: z ? "from the last closed candle" : "" };
  const away = 100 * (z.side === "demand" ? live - z.proximal : z.proximal - live) / z.proximal;
  const beyond = z.side === "demand" ? live < z.distal : live > z.distal;
  if (beyond) return { key: "ACTIVE", note: "price is beyond the zone" };
  if (away <= 0) return { key: "TOUCHING_NOW", note: "price is in the zone" };
  if (away <= state.reach) return { key: "APPROACHING", note: `${away.toFixed(2)}% away` };
  return { key: r.zone_state === "JUST_FORMED" ? "JUST_FORMED" : "ACTIVE", note: `${away.toFixed(1)}% away` };
}

// Under "location beats trend" the ITF is only asked when the price is in equilibrium.
const curveDecides = (r) => (r.direction_rule || "location") === "location" && r.market_state === "NORMAL" && ["VERY LOW", "LOW", "HIGH", "VERY HIGH"].includes(r.curve);

function zoneCell(z, tf) {
  if (!z) return el("td", { class: "muted" }, "None");
  return el("td", { class: "num" }, `${fmtPrice(z.proximal)} to ${fmtPrice(z.distal)}`,
    el("span", { class: "sub" }, `${TF_LABEL[tf] || ""}, score ${z.score} (${LABEL_TEXT[z.label] || z.label})`));
}

function shown() {
  const conf = Number($("f-conf").value);
  const sig = $("f-signal").value, st = $("f-state").value, scores = readScoreFilters(), type = $("f-type").value, q = $("f-search").value.trim().toUpperCase();
  return (state.board ? state.board.rows : []).map((r) => ({ r, live: liveState(r) })).filter(({ r, live }) => {
    const s = r.signal.signal;
    if (sig && (sig === "TRADE" ? s === "NO_TRADE" : s !== sig)) return false;
    if (st && (!r.ltf_zone || live.key !== st)) return false;
    if (!passesScores(r, scores)) return false;
    if (conf && !(r.confirmation && r.confirmation.pct != null && r.confirmation.pct >= conf)) return false;
    if (type && (type === "index") !== r.is_index) return false;
    return !q || r.symbol.toUpperCase().includes(q);
  });
}

function renderCounts(rows) {
  const trade = rows.filter(({ r }) => r.signal.signal !== "NO_TRADE");
  const n = { "": rows.length, BUY: trade.filter(({ r }) => r.signal.signal === "BUY").length, SELL: trade.filter(({ r }) => r.signal.signal === "SELL").length,
    APPROACHING: trade.filter(({ live }) => live.key === "APPROACHING").length, TOUCHING_NOW: trade.filter(({ live }) => live.key === "TOUCHING_NOW").length };
  const box = $("counts");
  box.replaceChildren();
  for (const [key, label] of [["", "Stocks shown"], ["BUY", "Buy"], ["SELL", "Sell"], ["APPROACHING", "Approaching"], ["TOUCHING_NOW", "Touching now"]]) {
    box.append(el("button", { "aria-pressed": String(state.pick === key), onclick: () => { state.pick = state.pick === key ? "" : key; render(); } },
      el("span", { class: "n" }, String(n[key])), el("span", { class: "l" }, label)));
  }
}

// The confirmation score: checks the zone rules do not make. On the dashboard it covers what stored data can answer;
// the Options page adds the checks that need the live option chain.
function confCell(r) {
  const c = r.confirmation;
  if (!c || c.pct == null) return el("td", { class: "muted" }, c ? "Nothing to check" : "");
  return el("td", {}, el("span", { class: "tag conf-" + c.label }, `${c.points} of ${c.max}`), el("span", { class: "sub" }, c.label));
}
function confLines(c) { return c.checks.map((x) => `${x.label}: ${x.applicable ? (x.max > 1 ? x.points + " of " + x.max : x.passed ? "yes" : "no") : "not checked"} (${x.note})`).join(". ") + "."; }

function planLine(p) {
  if (!p) return "not available";
  if (p.entry == null) return `${p.note}. Stop loss ${fmtPrice(p.stoploss)}; the target is ${p.rr} times the distance from the entry to the stop loss.`;
  return `entry ${fmtPrice(p.entry)}, stop loss ${fmtPrice(p.stoploss)}, target ${fmtPrice(p.target)} (${p.rr} to 1). ${p.note}`;
}
// What the entry of this score is waiting for, in a few words.
function entryNote(s) {
  const name = ENTRY_TEXT[s.primary_entry] || "";
  if (s.primary_entry === "TYPE_1") return `${name}, ${s.waiting ? "waiting for price to reach the zone" : "price has reached the zone"}, ${s.rr} to 1`;
  return s.waiting ? `${name}: no entry price yet, waits for its candles` : `${name}, entered, ${s.rr} to 1`;
}
function scoreLine(z) {
  return z ? `${fmtPrice(z.proximal)} to ${fmtPrice(z.distal)}: score ${z.score} = freshness ${z.freshness} + strength ${z.strength} (${z.strength_label}) + base ${z.base}${z.score > z.freshness + z.strength + z.base ? " + EMA 1" : ""}. ${z.pattern}, ${z.bases} base ${z.bases === 1 ? "candle" : "candles"}, ${testsText(z)}.` : "none near the price";
}

function detailRow(r) {
  const s = r.signal;
  const facts = [["Mode", modeText(r.mode)], ["Market state", lower(r.market_state)], ["HTF trend", lower(r.htf_trend)], ["ITF trend", `${lower(r.itf_trend)} (${r.itf_reason || "no reading"})`],
    ["Side decided by", curveDecides(r) ? `the curve: ${lower(r.curve)} is on the ${["VERY LOW", "LOW"].includes(r.curve) ? "demand side, so buy" : "supply side, so sell"}` : r.market_state !== "NORMAL" ? "the expansion rule (only one HTF zone is left)" : "the ITF trend"],
    [`${TF_LABEL[r.htf]} demand`, scoreLine(r.demand_zone)], [`${TF_LABEL[r.htf]} supply`, scoreLine(r.supply_zone)],
    [`${TF_LABEL[r.ltf]} entry zone`, r.ltf_zone ? scoreLine(r.ltf_zone) : (s.reason || "none")]];
  if (r.ltf_zone) facts.push(...ENTRY_KEYS.map(([k, label]) => [label + (s[k] && s[k].entry_type === s.primary_entry ? " (taken at this score)" : ""), planLine(s[k])]),
    ["Contact of the entry zone", contactText(r.contact_ltf)], ["Contact of the HTF zone", contactText(r.contact_htf)]);
  if (r.confirmation) facts.push([`Confirmation ${r.confirmation.points} of ${r.confirmation.max}`, confLines(r.confirmation) + " The Options page adds the checks that need the live option chain."]);
  if (s.signal === "NO_TRADE") facts.push(["Why no trade", s.reason || "not given"]);
  const dl = el("dl", { class: "kv" });
  for (const [k, v] of facts) dl.append(el("dt", {}, k), el("dd", {}, String(v)));
  return el("tr", { class: "more" }, el("td", { colspan: "11" }, dl));
}

function render() {
  const all = shown();
  renderCounts(all);
  const total = state.board ? state.board.rows.length : 0, f = readScoreFilters(), set = Object.values(f).some((v) => v > 0);
  const lost = state.board ? state.board.rows.filter((r) => !passesScores(r, f)).length : 0;
  $("hidden-line").textContent = set && total ? `Score filters: ${total - lost} of ${total} ready stocks pass. ${lost ? lost + " hidden; choose \"any\" or press Clear score filters to see them." : ""}` : "";
  const rank = { TOUCHING_NOW: 0, APPROACHING: 1, JUST_FORMED: 2, ACTIVE: 3 };
  const rows = all.filter(({ r, live }) => !state.pick || r.signal.signal === state.pick || (r.signal.signal !== "NO_TRADE" && live.key === state.pick))
    .sort((a, b) => Number(a.r.signal.signal === "NO_TRADE") - Number(b.r.signal.signal === "NO_TRADE") || (rank[a.live.key] ?? 9) - (rank[b.live.key] ?? 9) || b.r.signal.score - a.r.signal.score || a.r.symbol.localeCompare(b.r.symbol));
  const body = $("rows");
  body.replaceChildren();
  if (!rows.length) {
    const b = state.board;
    const why = !b ? "Loading" : !b.total ? "Your stock list is empty. Add stocks on the Settings page." : !b.ready ? "No stock is ready yet. Each one appears here as soon as its download and scan finish." : "No stock matches these filters. The score filters start at 7 or more; lower them or press Clear score filters.";
    body.append(el("tr", {}, el("td", { colspan: "11" }, why)));
    return;
  }
  const stale = state.status && state.status.feed.stale;
  for (const { r, live } of rows) {
    const s = r.signal, trade = s.signal !== "NO_TRADE", price = priceOf(r);
    const tr = el("tr", { class: "setup", onclick: () => { state.open = state.open === r.instrument_key ? null : r.instrument_key; render(); } },
      el("td", {}, r.symbol, el("span", { class: "sub" + (stale ? " stale" : "") }, price == null ? "no price" : fmtPrice(price))),
      zoneCell(r.demand_zone, r.htf), zoneCell(r.supply_zone, r.htf),
      el("td", {}, lower(r.curve), el("span", { class: "sub" }, r.market_state && r.market_state !== "NORMAL" ? lower(r.market_state) : `HTF ${lower(r.htf_trend)}`)),
      el("td", {}, lower(r.itf_trend), el("span", { class: "sub" }, TF_LABEL[r.itf] + (curveDecides(r) ? ", not used: the curve decides" : ""))),
      zoneCell(r.ltf_zone, r.ltf),
      el("td", {}, r.ltf_zone ? el("span", { class: "tag " + (STATE_CLASS[live.key] || "fresh") }, STATE_TEXT[live.key] || "") : "", el("span", { class: "sub" }, r.ltf_zone ? live.note : "")),
      el("td", {}, el("span", { class: "tag " + (s.signal === "BUY" ? "demand" : s.signal === "SELL" ? "supply" : "") }, s.signal === "BUY" ? "Buy" : s.signal === "SELL" ? "Sell" : "No trade"),
        el("span", { class: "sub wrap" }, trade ? `${LABEL_TEXT[s.zone_type] || ""}, score ${s.score}` : (s.reason || ""))),
      confCell(r),
      el("td", { class: "num" }, !trade ? "" : s.entry != null ? `${fmtPrice(s.entry)}, ${fmtPrice(s.stoploss)}, ${fmtPrice(s.target)}` : `zone at ${fmtPrice(s.watch)}, stop ${fmtPrice(s.stoploss)}`,
        el("span", { class: "sub wrap" }, trade ? entryNote(s) : "")),
      el("td", {}, el("div", { class: "rowbtns" },
        el("a", { class: "btn", href: `/chart?symbol=${encodeURIComponent(r.instrument_key)}&mode=${r.mode}`, onclick: (e) => e.stopPropagation() }, "Chart"),
        r.can_paper ? el("button", { class: "btn", onclick: (e) => { e.stopPropagation(); paperTrade(r); } }, "Paper trade") : "",
        r.ltf_zone ? el("a", { class: "btn", href: `/options?symbol=${encodeURIComponent(r.instrument_key)}&mode=${r.mode}`, onclick: (e) => e.stopPropagation() }, "Options") : "")));
    body.append(tr);
    if (state.open === r.instrument_key) body.append(detailRow(r));
  }
}

async function paperTrade(r) {
  try { const o = await post("/api/paper/orders", { zone_id: r.ltf_zone.zone_id, mode: r.mode }); say(o.entry_type && o.entry_type !== "TYPE_1" ? `Paper order placed for ${r.symbol}: ${lower(ENTRY_TEXT[o.entry_type])}, ${o.quantity} for now. It fills when ${ENTRY_HOW[o.entry_type]}; the quantity is then worked out again from the real entry. See the Paper page.`
      : `Paper order placed for ${r.symbol}: ${o.quantity} at ${fmtPrice(o.entry)}. See the Paper page.`); await loadBoard(); }
  catch (e) { say(`Could not place the paper order. ${e.message}`, true); }
}

function renderProgress() {
  const b = state.board, box = $("progress");
  if (!b || !b.total) { box.textContent = ""; return; }
  const parts = [`${b.ready} of ${b.total} stocks ready`];
  if (b.pending.length) parts.push(`still on their way: ${b.pending.slice(0, 8).map((p) => `${p.symbol} (${WORK_TEXT[p.state] || p.state}${p.state === "failed" && p.detail ? ": " + p.detail : ""})`).join(", ")}${b.pending.length > 8 ? ` and ${b.pending.length - 8} more` : ""}`);
  if (b.busy.length) parts.push(`updating now: ${b.busy.slice(0, 6).join(", ")}${b.busy.length > 6 ? ` and ${b.busy.length - 6} more` : ""}`);
  box.textContent = parts.join(". ") + ".";
  box.className = "status" + (b.pending.some((p) => p.state === "failed") ? " err" : "");
}

async function loadBoard() {
  state.board = await api(`/api/results?mode=${$("f-mode").value}`);
  state.reach = Number(state.board.approach_pct) || 1;
  renderProgress(); render();
}

function alertLine(a) {
  const what = { approaching: "approaching", triggered: "touching at", violated: "zone violated at", consumed: "zone used up at", entry2: "entry type 2 at", entry3: "entry type 3 at" }[a.alert] || a.alert;
  const side = a.side === "demand" ? "demand" : "supply";
  return el("li", {}, el("time", {}, String(a.time).replace("T", " ").slice(0, 16)), `${a.symbol} ${TF_LABEL[a.timeframe]} `, el("span", { class: side + "-t" }, side), ` zone: ${what} ${fmtPrice(a.price)}`);
}

async function loadStatus() {
  const s = await api(`/api/live/status?mode=${$("f-mode").value}`);
  state.status = s;
  Object.assign(state.prices, s.prices || {});
  $("market").textContent = s.market_open ? "Market open" : "Market closed"; $("market").className = "badge " + (s.market_open ? "ok" : "");
  $("token").textContent = s.has_token ? "Upstox token stored" : "Not logged in to Upstox"; $("token").className = "badge " + (s.has_token ? "ok" : "bad");
  const f = s.feed;
  $("feed").textContent = !s.scheduler ? "Schedule switched off" : f.error ? "Prices failing" : f.stale ? "Prices stale" : s.market_open ? "Prices live" : "Prices paused";
  $("feed").className = "badge " + (f.error || f.stale ? "bad" : s.market_open && s.scheduler ? "ok" : ""); $("feed").title = f.error || "";
  const list = $("alerts");
  list.replaceChildren();
  if (!s.alerts.length) list.append(el("li", { class: "empty" }, "No alerts yet. An alert appears when price approaches or touches an entry zone."));
  for (const a of s.alerts) list.append(alertLine(a));
  const parts = [s.last_scan ? `Last scan ${clock(s.last_scan)}` : "No scan since the app started"];
  if (s.next_bar_close) parts.push(`next 5 minute bar closes ${clock(s.next_bar_close)}`);
  if (f.last_poll) parts.push(`prices as of ${String(f.last_poll).replace("T", " ").slice(11, 19)}`);
  if (f.error) parts.push(f.error);
  $("foot").textContent = parts.join(". ") + ".";
}

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "prices") { Object.assign(state.prices, m.data); render(); }
    else if (m.type === "alert") { $("alerts").querySelector(".empty")?.remove(); $("alerts").prepend(alertLine(m)); }
    else if (m.type === "scan" || m.type === "ready") { loadBoard().catch(() => {}); if (m.type === "scan") loadStatus().catch(() => {}); }
  };
  ws.onclose = () => setTimeout(connect, 3000);   // reconnect on its own
}

(async function init() {
  for (const m of Object.keys(MODES)) $("f-mode").append(el("option", { value: m }, modeText(m)));
  const q = new URLSearchParams(location.search);
  $("f-mode").value = MODES[q.get("mode")] ? q.get("mode") : "MONTHLY";      // Monthly unless a link says otherwise
  $("f-mode").onchange = () => { state.open = null; loadBoard().catch((e) => say(e.message, true)); loadStatus().catch(() => {}); };
  for (const id of ["f-signal", "f-state", "f-type"]) $(id).onchange = render;
  try { $("f-conf").value = localStorage.getItem("zoneScanner.confFilter") || ""; } catch (e) { /* storage not available */ }
  $("f-conf").onchange = () => { try { localStorage.setItem("zoneScanner.confFilter", $("f-conf").value); } catch (e) { /* storage not available */ } render(); };
  buildScoreFilters($("score-filters"), render);
  $("f-search").oninput = render;
  try { await loadStatus(); await loadBoard(); } catch (e) { say(`Could not load the dashboard. ${e.message}`, true); }
  connect();
  setInterval(() => { loadStatus().catch(() => {}); if (state.board && state.board.pending.length) loadBoard().catch(() => {}); }, 15000);
})();