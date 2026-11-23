// Option paper page. Orders and positions come from the database; prices and events arrive over /ws/live.
const $ = (id) => document.getElementById(id);
const stamp = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "");
function say(text, isError = false) { $("status").textContent = text; $("status").className = "status" + (isError ? " err" : ""); }
const prices = {};
let data = null, lastPrice = 0;
const SETTINGS = ["opt_paper_capital", "opt_paper_risk_pct", "opt_fixed_lots", "opt_max_risk", "opt_strike_rule", "opt_min_days", "opt_max_premium_pct", "opt_expiry_close",
  "opt_premium_stop_pct"];
const WHY = { stop: "Stop (stock)", trail: "Trailing stop", target: "Target (stock)", "premium stop": "Premium stop", expiry: "Expiry day", manual: "Closed by you" };
const sigTag = (s) => el("span", { class: "tag " + (s === "BUY" ? "demand" : "supply") }, s === "BUY" ? "BUY → CE" : "SELL → PE");
const tfOf = (x) => tfText(x.mode, x.timeframe);
const spotOf = (x) => prices[x.underlying_key] ?? x.last_spot;
const premOf = (x) => prices[x.option_key] ?? x.last_premium;

function figures() {
  const s = data.summary;
  const openPnl = data.open.reduce((a, t) => a + (premOf(t) != null ? (premOf(t) - t.fill_premium) * t.quantity : 0), 0);
  const rows = [["Balance", money(s.balance)], ["Free money", money(s.free)], ["In positions", money(s.in_positions)],
    ["Open profit or loss", signed(openPnl)], ["Closed, after charges", signed(s.closed_pnl)],
    ["Trades won", s.trades ? `${s.wins} of ${s.trades}` : "none yet"]];
  $("figures").replaceChildren(...rows.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, String(v)))));
  const age = lastPrice ? Math.round((Date.now() - lastPrice) / 1000) : null;
  $("feed").textContent = !data.market_open ? "⚪ Market closed: prices are the last ones seen; nothing fills until the open."
    : age == null ? "🟡 Waiting for live prices…" : age <= 15 ? `🟢 Live · last price ${age} s ago` : `🔴 No price for ${age} s: reconnecting`;
}

// The Trailing / locked cell: the same lines and step ladder as the Paper page, on the stock's price, with what the
// stop keeps in rupees of the option (its estimated premium at the stop).
function asStock(t) { return { side: t.signal === "BUY" ? "demand" : "supply", fill_price: t.fill_spot, stop: t.stop, trail_view: t.lock || {} }; }
function trailCell(t, spot) {
  const tv = trailLines(asStock(t), spot, 0.05);
  const off = !t.lock || !t.lock.method || t.lock.method === "off";
  return { tv, cell: el("td", { class: "trail-cell" },
    ...tv.gold.map((g) => el("span", { class: "trail-gold" }, g)),
    el("span", { class: "trail-head" }, tv.lines[0]),
    ...(tv.ladder ? [ladderBar(tv.ladder)] : []),
    ...tv.lines.slice(1).map((x) => el("span", { class: "sub wrap" }, x)),
    t.lock && t.lock.premium_at_stop != null ? el("span", { class: "sub wrap" }, `Option at the stop: about ${fmtPrice(t.lock.premium_at_stop)}`) : "",
    off ? rowBtn("Apply trailing from Settings", () => act(`Trailing applied to ${t.option_label}.`, () => post(`/api/option-paper/${t.id}/trail`)), "btn trail-apply") : "") };
}
function ladderBar(L) {
  const bar = el("div", { class: "ladder", role: "img", "aria-label": `Steps from the entry ${L.entry} to the target ${L.target}` });
  bar.append(el("div", { class: "ladder-fill", style: `width:${(L.filled * 100).toFixed(1)}%` }));
  for (const tk of L.ticks) bar.append(el("span", { class: "ladder-tick " + tk.kind, style: `left:${(tk.x * 100).toFixed(1)}%` }));
  bar.append(el("span", { class: "ladder-tick target", style: "left:100%" }));
  if (L.dot != null) bar.append(el("span", { class: "ladder-dot" + (L.losing ? " losing" : ""), style: `left:${(L.dot * 100).toFixed(1)}%` }));
  return el("div", { class: "ladder-wrap" }, bar, el("div", { class: "ladder-ends" }, el("span", {}, L.entry), el("span", {}, `🎯 ${L.target}`)));
}

// Pending orders: how near the stock is to the entry, in R (entry to stop), like the Paper page.
const history = {};
const PICK_KEY = "zoneScanner.optionNear";
let pick = (() => { try { return localStorage.getItem(PICK_KEY) || "all"; } catch (e) { return "all"; } })();
const asOrder = (o) => ({ entry: o.entry, stop: o.stop, side: o.signal === "BUY" ? "demand" : "supply" });

function rowBtn(label, fn, cls = "btn") { return el("button", { class: cls, onclick: fn }, label); }

// "18.00" with "last close" under it when the market is closed
function premCell(x) {
  const p = premOf(x);
  return el("td", { class: "num" }, p == null ? "no price" : el("strong", {}, fmtPrice(p)),
    data.market_open ? "" : el("span", { class: "sub" }, "last close"));
}
const est = (v, level, word) => el("td", { class: "num" }, v == null ? "—" : fmtPrice(v), level == null ? "" : el("span", { class: "sub" }, `${word} ${fmtPrice(level)}`));
const optCell = (x) => el("td", {}, el("strong", {}, x.option_label), el("span", { class: "sub" }, `${x.signal} · ${tfOf(x)}`));
const strikeCell = (x) => el("td", { class: "num" }, el("strong", {}, fmtPrice(x.strike)), el("span", { class: "sub" }, x.option_type || ""));

function render() {
  if (!data) return;
  figures();
  const open = $("open"); open.replaceChildren();
  if (!data.open.length) open.append(el("tr", {}, el("td", { colspan: "11" }, "No open option positions.")));
  for (const t of data.open) {
    const p = premOf(t), sp = spotOf(t), pnl = p == null ? null : (p - t.fill_premium) * t.quantity;
    const chg = p == null ? null : 100 * (p - t.fill_premium) / t.fill_premium;
    const r = pnl != null && t.risk_amount ? pnl / t.risk_amount : null;
    const tc = trailCell(t, sp);
    const cls = [tc.tv.locked ? "pos-locked" : "", tc.tv.nearStop ? "near-stop" : ""].filter(Boolean).join(" ");
    open.append(el("tr", { class: cls },
      el("td", {}, el("strong", {}, t.option_label), el("span", { class: "sub" }, `${t.signal} · ${tfOf(t)}`),
        tc.tv.nearStop ? el("span", { class: "near-stop-label" }, "near the stop") : ""),
      strikeCell(t),
      el("td", { class: "when" }, el("strong", {}, fmtPrice(t.fill_premium)), el("span", { class: "sub" }, `${stamp(t.fill_ts)}, stock ${fmtPrice(t.fill_spot)}`)),
      el("td", { class: "num" }, p == null ? "no price" : el("strong", {}, fmtPrice(p)),
        el("span", { class: "sub " + (chg > 0 ? "win" : chg < 0 ? "loss" : "") }, chg == null ? "" : `${chg > 0 ? "+" : ""}${chg.toFixed(1)}%${data.market_open ? "" : " · last close"}`)),
      el("td", { class: "num" }, t.est_stop_now == null ? "—" : fmtPrice(t.est_stop_now) + (t.stop !== t.first_stop ? " 🔒" : ""),
        el("span", { class: "sub" }, t.stop !== t.first_stop ? `was ${fmtPrice(t.est_stop)}` : `when stock ${fmtPrice(t.stop)}`)),
      est(t.est_target_now, t.target, "when stock"),
      el("td", { class: "num " + (pnl > 0 ? "win" : pnl < 0 ? "loss" : "") }, pnl == null ? "" : el("strong", {}, signed(pnl)),
        el("span", { class: "sub" }, r == null ? "" : `${r > 0 ? "+" : ""}${r.toFixed(2)} R`)),
      el("td", { class: "num" }, `${sp == null ? "" : fmtPrice(sp)} / ${fmtPrice(t.stop)}${t.stop !== t.first_stop ? " 🔒" : ""} / ${fmtPrice(t.target)}`),
      tc.cell,
      el("td", {}, durationText(t.open_s), el("span", { class: "sub" }, `${t.lots} × ${t.lot_size}`)),
      el("td", {}, rowBtn("Close now", () => act("Position closed at the option's last price.", () => post(`/api/option-paper/${t.id}/close`))))));
  }
  renderPending();
  const sig = $("signals"); sig.replaceChildren();
  if (!data.signals.length) sig.append(el("tr", {}, el("td", { colspan: "10" }, "No signal on a stock with options right now. Signals appear after a scan finds a fresh zone the curve and trend allow; stocks without options are not shown here.")));
  for (const s of data.signals) {
    const v = previews[s.plan_id];
    const btn = s.placed ? el("span", { class: "sub" }, "order placed") : rowBtn("Place option order", async (ev) => {
      ev.target.disabled = true;
      await act(`Option order placed for ${s.symbol}.`, () => post("/api/option-paper/place", { plan_id: s.plan_id }));
      ev.target.disabled = false;
    });
    const optionCells = s.placed ? [el("td", { colspan: "5" }, el("span", { class: "sub" }, "see Pending orders or Open positions"))]
      : !v ? [el("td", { colspan: "5" }, el("span", { class: "sub" }, previewsLoading ? "choosing the option…" : "not checked yet: it is checked on the next refresh"))]
      : v.skipped && !v.strike ? [el("td", { colspan: "5" }, el("span", { class: "sub loss" }, `skipped: ${v.skipped}`))]
      : [el("td", {}, `${v.expiry ? v.expiry.slice(8) + " " + new Date(v.expiry).toLocaleString("en", { month: "short" }) : ""} ${v.option_type || ""}`,
           el("span", { class: "sub" }, v.days_left == null ? "" : `${v.days_left} days`)),
         el("td", { class: "num" }, el("strong", {}, fmtPrice(v.strike))),
         el("td", { class: "num" }, (prices[v.option_key] ?? v.premium) == null ? "" : el("strong", {}, fmtPrice(prices[v.option_key] ?? v.premium)),
           el("span", { class: "sub" }, !data.market_open ? "last close" : prices[v.option_key] != null ? "live" : "from the chain")),
         el("td", { class: "num" }, v.est_entry == null ? "" : fmtPrice(v.est_entry), el("span", { class: "sub" }, `stop ${fmtPrice(v.est_stop)} / target ${fmtPrice(v.est_target)}`)),
         el("td", { class: "num" }, v.skipped ? el("span", { class: "sub loss" }, `0 · ${v.skipped.split(": ").slice(1).join(": ")}`) : `${v.lots} × ${v.lot}`,
           v.cost ? el("span", { class: "sub" }, `≈ ₹${money(v.cost)}, risk ₹${money(v.risk)}`) : "")];
    sig.append(el("tr", {}, el("td", {}, el("strong", {}, s.symbol), el("span", { class: "sub" }, tfOf(s))), el("td", {}, sigTag(s.signal)),
      el("td", { class: "num" }, String(s.score ?? "")),
      el("td", { class: "num" }, `${fmtPrice(s.entry)} / ${fmtPrice(s.stop)} / ${fmtPrice(s.target)}`,
        el("span", { class: "sub" }, `now ${fmtPrice(prices[s.instrument_key] ?? s.price)} · ${ENTRY_TEXT[s.entry_type] || ""}`)),
      ...optionCells, el("td", {}, btn)));
  }
  const closed = $("closed"); closed.replaceChildren();
  if (!data.closed.length) closed.append(el("tr", {}, el("td", { colspan: "12" }, "No closed option trades yet.")));
  for (const t of data.closed) {
    if (t.status === "cancelled") {
      closed.append(el("tr", { class: "cancelled" }, optCell(t), strikeCell(t), el("td", { colspan: "10" }, `Cancelled ${stamp(t.created_at)}: ${t.note || ""}`)));
      continue;
    }
    closed.append(el("tr", {},
      optCell(t), strikeCell(t),
      el("td", { class: "num" }, el("strong", {}, `${fmtPrice(t.fill_premium)} → ${fmtPrice(t.exit_premium)}`)),
      el("td", { class: "num " + (t.change_pct > 0 ? "win" : "loss") }, t.change_pct == null ? "" : `${t.change_pct > 0 ? "+" : ""}${t.change_pct}%`),
      el("td", { class: "num" }, `${fmtPrice(t.fill_spot)} → ${fmtPrice(t.exit_spot)}`),
      el("td", {}, WHY[t.exit_reason] || t.exit_reason || ""),
      el("td", { class: "when" }, stamp(t.fill_ts), el("span", { class: "sub" }, `out ${stamp(t.exit_ts)}`)),
      el("td", {}, durationText(t.open_s), t.waited_s >= 60 ? el("span", { class: "sub" }, `waited ${durationText(t.waited_s)} for the fill`) : ""),
      el("td", { class: "num" }, `${t.lots} × ${t.lot_size}`),
      el("td", { class: "num" }, fmtPrice(t.charges)),
      el("td", { class: "num " + (t.pnl_after > 0 ? "win" : "loss") }, el("strong", {}, signed(t.pnl_after)), el("span", { class: "sub" }, `before charges ${signed(t.pnl)}`)),
      el("td", { class: "num" }, t.result_r == null ? "" : `${t.result_r > 0 ? "+" : ""}${t.result_r} R`)));
  }
}

// The option each signal would get, asked a few signals at a time so the first ones show quickly.
function renderPending() {
  const now = Date.now(), closedMarket = !data.market_open;
  const rows = data.pending.map((o) => {
    const sp = spotOf(o), d = entryDistance(asOrder(o), sp);
    const speed = closedMarket ? { text: "market closed" } : entrySpeed(asOrder(o), history[o.underlying_key], now);
    return { o, sp, d, speed };
  }).sort((a, b) => (a.d ? a.d.r : 1e9) - (b.d ? b.d.r : 1e9));
  const counts = { all: rows.length };
  for (const g of NEAR_GROUPS) counts[g.key] = rows.filter((x) => x.d && x.d.group === g.key).length;
  const chips = $("near-chips"); chips.replaceChildren();
  for (const [key, text] of [...NEAR_GROUPS.map((g) => [g.key, `${g.icon} ${g.label}`]), ["all", "All"]]) {
    chips.append(el("button", { class: "chip near-" + key, "aria-pressed": String(pick === key),
      onclick: () => { pick = key; try { localStorage.setItem(PICK_KEY, key); } catch (e) { /* storage not available */ } renderPending(); } }, `${text} ${counts[key]}`));
  }
  const pend = $("pending"); pend.replaceChildren();
  const shown = rows.filter((x) => pick === "all" || (x.d && x.d.group === pick));
  if (!data.pending.length) pend.append(el("tr", {}, el("td", { colspan: "13" }, "No pending option orders.")));
  else if (!shown.length) pend.append(el("tr", {}, el("td", { colspan: "13" }, "No pending order in this group right now. Press All to see every order.")));
  const delay = `${-(now % 1600)}ms`;
  for (const { o, sp, d, speed } of shown) {
    const g = d ? NEAR_GROUPS.find((x) => x.key === d.group) : null;
    const dist = !d ? el("td", { class: "near" }, el("span", { class: "sub" }, "no live price yet"))
      : el("td", { class: "near" }, el("span", { class: "near-badge near-" + d.group, style: d.group === "now" ? `animation-delay:${delay}` : "" }, `${g.icon} ${g.label.toUpperCase()}`),
        el("span", { class: "near-line" }, d.at ? "stock is at the entry" : `${d.r} R · ${d.pct}% away`),
        el("span", { class: "near-bar" }, el("span", { style: `width:${(d.fill * 100).toFixed(0)}%` })),
        el("span", { class: "sub" }, `${sp == null ? "" : fmtPrice(sp)} → ${fmtPrice(o.entry)} · ${speed.text}`));
    pend.append(el("tr", { class: d ? "near-row near-" + d.group : "" },
      optCell(o), dist, strikeCell(o),
      el("td", {}, o.expiry || "", el("span", { class: "sub" }, o.days_left == null ? "" : `${o.days_left} days`)),
      premCell(o),
      est(o.est_entry_now ?? o.est_entry, o.entry, "when stock"),
      est(o.est_stop_now ?? o.est_stop, o.stop, "when stock"),
      est(o.est_target_now ?? o.est_target, o.target, "when stock"),
      el("td", { class: "num" }, `${o.lots} × ${o.lot_size}`, el("span", { class: "sub" }, `≈ ₹${money(o.cost)}`)),
      el("td", { class: "num" }, `₹${money(o.risk_amount)}`),
      el("td", { class: "num" }, `${fmtPrice(o.entry)} / ${fmtPrice(o.stop)} / ${fmtPrice(o.target)}`,
        o.note && o.note.startsWith("waiting") ? el("span", { class: "sub loss" }, o.note) : el("span", { class: "sub" }, ENTRY_TEXT[o.entry_type] || "")),
      el("td", { class: "when" }, stamp(o.created_at), el("span", { class: "sub" }, o.placed_by === "auto" ? (o.note || "auto") : "by you")),
      el("td", {}, rowBtn("Cancel", () => act("Order cancelled.", () => post(`/api/option-paper/${o.id}/close`))))));
  }
}

let previews = {}, previewsLoading = false, previewError = null;
async function loadPreviews() {
  if (previewsLoading || !data) return;
  previewsLoading = true; previewError = null; render();
  const ids = data.signals.filter((s) => !s.placed).map((s) => s.plan_id);
  for (let i = 0; i < ids.length; i += 5) {
    try { Object.assign(previews, await api(`/api/option-paper/previews?plan_ids=${ids.slice(i, i + 5).join(",")}`)); }
    catch (e) { previewError = e.message; for (const id of ids.slice(i, i + 5)) previews[id] = previews[id] || { skipped: `could not check the option chain: ${e.message}` }; }
    render();
  }
  previewsLoading = false; render();
}

async function load() {
  data = await api("/api/option-paper/positions");
  const s = data.settings;
  for (const k of SETTINGS) if (document.activeElement !== $(k)) $(k).value = s[k] ?? "";
  $("opt_lot_mode").value = s.opt_lot_mode === "risk" ? "risk" : "fixed";
  $("opt_days_rule").value = s.opt_days_rule === "one" ? "one" : "mode";
  if (!$("mode-table").contains(document.activeElement)) fillModeTable(data.mode_tables);
  $("opt_auto").checked = Boolean(s.opt_auto);
  const score = String(Math.round(Number(s.opt_auto_min_score ?? 8)));
  if (![...$("opt_auto_min_score").options].some((o) => o.value === score)) $("opt_auto_min_score").append(el("option", {}, score));
  $("opt_auto_min_score").value = score;
  $("opt_auto_closed").checked = Boolean(s.opt_auto_closed);
  render();
}
async function act(done, fn) { try { await fn(); say(done); await load(); } catch (e) { say(e.message, true); } }

$("opt-save").onclick = () => {
  previews = {};
  const body = { opt_lot_mode: $("opt_lot_mode").value, opt_days_rule: $("opt_days_rule").value, ...modeTables(), opt_strike_rule: $("opt_strike_rule").value, opt_expiry_close: $("opt_expiry_close").value.trim() };
  for (const k of SETTINGS) if (!(k in body)) body[k] = Number($(k).value);
  act("Option settings saved. New orders use them; orders already placed keep their strike and lots.",
    () => api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }));
};
// Delta and minimum days for each mode (Option paper settings).
const MODE_ORDER = ["30M", "HOURLY", "2HR", "4HR", "DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "HALFYEARLY", "YEARLY"];
function fillModeTable(t) {
  if (!t) return;
  $("mode-table").replaceChildren(...MODE_ORDER.map((m) => el("tr", {}, el("td", {}, MODE_LABEL[m] || m),
    el("td", {}, el("input", { type: "number", min: "0.05", max: "0.95", step: "0.05", value: t.delta[m], "data-mode": m, "data-kind": "delta", "aria-label": `Target delta, ${MODE_LABEL[m] || m}` })),
    el("td", {}, el("input", { type: "number", min: "0", max: "365", step: "1", value: t.min_days[m], "data-mode": m, "data-kind": "days", "aria-label": `Minimum days, ${MODE_LABEL[m] || m}` })))));
}
function modeTables() {
  const delta = {}, days = {};
  for (const i of $("mode-table").querySelectorAll("input")) {
    if (i.value === "") continue;
    (i.dataset.kind === "delta" ? delta : days)[i.dataset.mode] = Number(i.value);
  }
  return { opt_mode_delta: delta, opt_mode_min_days: days };
}

// Automatic option orders: saved as soon as they change, like the Paper page.
const putSettings = (body) => api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const saveAuto = () => act($("opt_auto").checked
    ? `Automatic option orders on for score ${$("opt_auto_min_score").value} and above (market hours).`
    : "Automatic option orders off.",
  () => putSettings({ opt_auto: $("opt_auto").checked, opt_auto_min_score: Number($("opt_auto_min_score").value) }));
$("opt_auto").onchange = saveAuto; $("opt_auto_min_score").onchange = saveAuto;
$("opt_auto_closed").onchange = () => act($("opt_auto_closed").checked
    ? "Automatic option orders will also be placed after a scan while the market is closed."
    : "Automatic option orders only in market hours.",
  () => putSettings({ opt_auto_closed: $("opt_auto_closed").checked }));

$("auto-run").onclick = async () => {
  const btn = $("auto-run"); btn.disabled = true;
  try {
    const r = await post("/api/option-paper/auto/run");
    const box = $("auto-result"); box.hidden = false;
    box.replaceChildren(
      el("p", { class: "auto-head" }, `${r.placed.length} option ${r.placed.length === 1 ? "order" : "orders"} placed${r.placed.length ? ": " + r.placed.map((x) => x.label).join(", ") : ""}`),
      ...(r.skipped.length ? [el("p", { class: "sub" }, `${r.skipped.length} skipped:`),
        el("ul", { class: "auto-skips" }, ...r.skipped.slice(0, 30).map((x) => el("li", {}, `${x.symbol} (${MODE_LABEL[x.mode] || x.mode}): ${x.why}`)))] : []),
      el("button", { class: "btn", onclick: () => { box.hidden = true; } }, "Hide"));
    await load();
  } catch (e) { say(e.message, true); } finally { btn.disabled = false; }
};
$("reset").onclick = () => { if (confirm("Archive every option paper order and trade and start again from the capital?")) act("Option paper account reset.", () => post("/api/option-paper/reset")); };

let reloadTimer = null;
const reloadSoon = () => { clearTimeout(reloadTimer); reloadTimer = setTimeout(() => load().catch(() => {}), 1000); };   // many events: one reload
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`);
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "prices") {
      Object.assign(prices, m.data); lastPrice = Date.now();
      if (data) for (const o of data.pending) trackPrice(history, o.underlying_key, prices[o.underlying_key], lastPrice);
      render();
    }
    else if (m.type === "option-paper" || m.type === "scan") reloadSoon();
  };
  ws.onclose = () => setTimeout(connect, 3000);
}
setInterval(() => { if (data) figures(); }, 5000);                                    // the live indicator's age
setInterval(() => { if (!document.hidden) load().catch(() => {}); }, 30000);          // anything the price feed missed
showToken($("token"));
load().then(loadPreviews).catch((e) => say(`Could not load option paper trading. ${e.message}`, true));
setInterval(() => { if (!document.hidden) loadPreviews(); }, 5 * 60 * 1000);           // the chain is cached 15 min on the server
connect();
