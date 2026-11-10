// Option paper page. Orders and positions come from the database; prices and events arrive over /ws/live.
const $ = (id) => document.getElementById(id);
const stamp = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "");
function say(text, isError = false) { $("status").textContent = text; $("status").className = "status" + (isError ? " err" : ""); }
const prices = {};
let data = null, lastPrice = 0;
const SETTINGS = ["opt_paper_capital", "opt_paper_risk_pct", "opt_strike_rule", "opt_min_days", "opt_max_premium_pct", "opt_expiry_close",
  "opt_premium_stop_pct", "opt_auto_min_score"];
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

function lockText(t) {
  const k = t.lock;
  if (!k) return (t.trail && t.trail.trail_method) ? `${t.trail.trail_method}: stop ${fmtPrice(t.stop)}` : "No trailing";
  const head = k.steps > 0 ? `🔒 Step ${k.steps} of ${k.steps_to_target}` : `🔓 Not locked yet · Step 0 of ${k.steps_to_target}`;
  return [head, el("span", { class: "sub" }, `next: stop ${fmtPrice(k.next_stop)} when the stock reaches ${fmtPrice(k.next_trigger)}`)];
}

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
    open.append(el("tr", {},
      optCell(t), strikeCell(t),
      el("td", { class: "when" }, el("strong", {}, fmtPrice(t.fill_premium)), el("span", { class: "sub" }, `${stamp(t.fill_ts)}, stock ${fmtPrice(t.fill_spot)}`)),
      el("td", { class: "num" }, p == null ? "no price" : el("strong", {}, fmtPrice(p)),
        el("span", { class: "sub " + (chg > 0 ? "win" : chg < 0 ? "loss" : "") }, chg == null ? "" : `${chg > 0 ? "+" : ""}${chg.toFixed(1)}%${data.market_open ? "" : " · last close"}`)),
      el("td", { class: "num" }, t.est_stop_now == null ? "—" : fmtPrice(t.est_stop_now) + (t.stop !== t.first_stop ? " 🔒" : ""),
        el("span", { class: "sub" }, t.stop !== t.first_stop ? `was ${fmtPrice(t.est_stop)}` : `when stock ${fmtPrice(t.stop)}`)),
      est(t.est_target_now, t.target, "when stock"),
      el("td", { class: "num " + (pnl > 0 ? "win" : pnl < 0 ? "loss" : "") }, pnl == null ? "" : el("strong", {}, signed(pnl)),
        el("span", { class: "sub" }, r == null ? "" : `${r > 0 ? "+" : ""}${r.toFixed(2)} R`)),
      el("td", { class: "num" }, `${sp == null ? "" : fmtPrice(sp)} / ${fmtPrice(t.stop)}${t.stop !== t.first_stop ? " 🔒" : ""} / ${fmtPrice(t.target)}`),
      el("td", {}, ...[].concat(lockText(t))),
      el("td", {}, durationText(t.open_s), el("span", { class: "sub" }, `${t.lots} × ${t.lot_size}`)),
      el("td", {}, rowBtn("Close now", () => act("Position closed at the option's last price.", () => post(`/api/option-paper/${t.id}/close`))))));
  }
  const pend = $("pending"); pend.replaceChildren();
  if (!data.pending.length) pend.append(el("tr", {}, el("td", { colspan: "12" }, "No pending option orders.")));
  for (const o of data.pending) {
    const sp = spotOf(o), sign = o.signal === "BUY" ? 1 : -1;
    const away = sp == null ? null : 100 * sign * (sp - o.entry) / o.entry;
    pend.append(el("tr", {},
      optCell(o), strikeCell(o),
      el("td", {}, o.expiry || "", el("span", { class: "sub" }, o.days_left == null ? "" : `${o.days_left} days`)),
      premCell(o),
      est(o.est_entry_now ?? o.est_entry, o.entry, "when stock"),
      est(o.est_stop_now ?? o.est_stop, o.stop, "when stock"),
      est(o.est_target_now ?? o.est_target, o.target, "when stock"),
      el("td", { class: "num" }, `${o.lots} × ${o.lot_size}`, el("span", { class: "sub" }, `≈ ₹${money(o.cost)}`)),
      el("td", { class: "num" }, `₹${money(o.risk_amount)}`),
      el("td", { class: "num" }, `${sp == null ? "" : fmtPrice(sp)} → ${fmtPrice(o.entry)}`,
        el("span", { class: "sub" }, away == null ? "" : away <= 0 ? "at the entry" : `${away.toFixed(2)}% away`), o.note && o.note.startsWith("waiting") ? el("span", { class: "sub loss" }, o.note) : ""),
      el("td", { class: "when" }, stamp(o.created_at), el("span", { class: "sub" }, o.placed_by === "auto" ? (o.note || "auto") : "by you")),
      el("td", {}, rowBtn("Cancel", () => act("Order cancelled.", () => post(`/api/option-paper/${o.id}/close`))))));
  }
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
  $("opt_auto").checked = Boolean(s.opt_auto);
  $("opt_auto_closed").checked = Boolean(s.opt_auto_closed);
  render();
}
async function act(done, fn) { try { await fn(); say(done); await load(); } catch (e) { say(e.message, true); } }

$("opt-save").onclick = () => {
  previews = {};
  const body = { opt_auto: $("opt_auto").checked, opt_auto_closed: $("opt_auto_closed").checked, opt_strike_rule: $("opt_strike_rule").value, opt_expiry_close: $("opt_expiry_close").value.trim() };
  for (const k of SETTINGS) if (!(k in body)) body[k] = Number($(k).value);
  act("Option settings saved. New orders use them; orders already placed keep their strike and lots.",
    () => api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }));
};
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
    if (m.type === "prices") { Object.assign(prices, m.data); lastPrice = Date.now(); render(); }
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
