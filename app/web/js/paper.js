// Paper trading page. Positions come from the database; prices and fills arrive over /ws/live.
const $ = (id) => document.getElementById(id);
let data = { pending: [], open: [], closed: [] }, summary = null, prices = {};
const stamp = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "");
const sideTag = (t) => el("span", { class: "tag " + t.side }, t.kind === "option" ? "Option" : t.side === "demand" ? "Long" : "Short");
function say(text, isError = false) { $("status").textContent = text; $("status").className = "status" + (isError ? " err" : ""); }
const livePnl = (t) => { const p = prices[t.instrument_key] ?? t.price; return p == null ? null : (p - t.fill_price) * (t.side === "demand" ? 1 : -1) * t.quantity; };

function renderFigures() {
  const s = summary, fig = $("figures");
  fig.replaceChildren();
  const open = data.open.reduce((sum, t) => sum + (livePnl(t) ?? 0), 0);
  const rows = [["Balance", money(s.balance)], ["Closed profit or loss", signed(s.realised)], ["Open profit or loss", signed(open)],
    ["Closed trades", s.closed_trades], ["Win rate", s.win_rate == null ? "none yet" : `${s.win_rate}%`], ["Expectancy per trade", s.expectancy_r == null ? "none yet" : `${s.expectancy_r} R`],
    ["Risk per trade", `${s.risk_pct}% of capital`]];
  for (const [k, v] of rows) fig.append(el("div", {}, el("dt", {}, k), el("dd", {}, String(v))));
}

function render() {
  renderFigures();
  const open = $("open"); open.replaceChildren();
  if (!data.open.length) open.append(el("tr", {}, el("td", { colspan: "9" }, "No open positions. An entry type 1 order becomes a position when the live price reaches its entry; an entry type 2 or 3 order when its candles have closed.")));
  for (const t of data.open) {
    const p = prices[t.instrument_key] ?? t.price, pnl = livePnl(t), zone = t.kind !== "option";
    const stop = zone ? el("input", { class: "edit", type: "number", step: "0.05", value: t.stop, "aria-label": "Stop" }) : "";
    const target = zone ? el("input", { class: "edit", type: "number", step: "0.05", value: t.target, "aria-label": "Target" }) : "";
    open.append(el("tr", {}, el("td", {}, t.symbol), el("td", {}, sideTag(t)), el("td", { class: "num" }, String(t.quantity)),
      el("td", {}, `${fmtPrice(t.fill_price)} at ${stamp(t.fill_ts)}`), el("td", {}, stop), el("td", {}, target), el("td", { class: "num" }, p == null ? "no live price" : fmtPrice(p)),
      el("td", { class: "num " + (pnl > 0 ? "win" : pnl < 0 ? "loss" : "") }, pnl == null ? "" : signed(pnl)),
      el("td", {}, el("div", { class: "rowbtns" },
        zone ? el("button", { class: "btn", onclick: () => act(`Stop and target saved for ${t.symbol}.`, () => api(`/api/paper/positions/${t.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ stop: Number(stop.value), target: Number(target.value) }) })) }, "Save") : "",
        el("button", { class: "btn", onclick: () => act(`${t.symbol} closed.`, () => post(`/api/paper/positions/${t.id}/close`)) }, "Close now")))));
  }
  const pend = $("pending"); pend.replaceChildren();
  if (!data.pending.length) pend.append(el("tr", {}, el("td", { colspan: "10" }, "No pending orders. Use Paper trade on a setup in the scanner, or switch on automatic orders above.")));
  for (const o of data.pending) {
    const p = prices[o.instrument_key] ?? o.price;
    pend.append(el("tr", {}, el("td", {}, o.symbol), el("td", {}, TF_LABEL[o.timeframe] || ""), el("td", {}, sideTag(o)), el("td", { class: "num" }, String(o.quantity)),
      el("td", {}, ENTRY_TEXT[o.entry_type] || "Entry type 1", el("span", { class: "sub wrap" }, o.entry_type === "TYPE_2" || o.entry_type === "TYPE_3" ? `waits until ${ENTRY_HOW[o.entry_type]}` : "limit at the proximal line")),
      el("td", { class: "num" }, fmtPrice(o.entry), el("span", { class: "sub" }, o.entry_type === "TYPE_2" || o.entry_type === "TYPE_3" ? "proximal line, until the entry" : "")),
      el("td", { class: "num" }, fmtPrice(o.stop)), el("td", { class: "num" }, fmtPrice(o.target)), el("td", { class: "num" }, p == null ? "" : fmtPrice(p)),
      el("td", {}, el("button", { class: "btn", onclick: () => act(`Order for ${o.symbol} cancelled.`, () => api(`/api/paper/orders/${o.id}`, { method: "DELETE" })) }, "Cancel order"))));
  }
  const closed = $("closed"); closed.replaceChildren();
  if (!data.closed.length) closed.append(el("tr", {}, el("td", { colspan: "10" }, "No closed trades yet.")));
  const why = { stop: "Stop", target: "Target", manual: "Closed by you" };
  for (const t of data.closed) {
    closed.append(el("tr", {}, el("td", {}, t.symbol), el("td", {}, sideTag(t)), el("td", { class: "num" }, String(t.quantity)), el("td", {}, stamp(t.fill_ts)), el("td", {}, stamp(t.exit_ts)),
      el("td", { class: "num" }, fmtPrice(t.fill_price)), el("td", { class: "num" }, fmtPrice(t.exit_price)), el("td", {}, why[t.exit_reason] || ""),
      el("td", { class: "num " + (t.result === "win" ? "win" : "loss") }, signed(t.pnl)), el("td", { class: "num" }, t.result_r == null ? "" : `${t.result_r > 0 ? "+" : ""}${t.result_r} R`)));
  }
}

async function load() {
  [data, summary] = await Promise.all([api("/api/paper/positions"), api("/api/paper/summary")]);
  $("auto").checked = summary.auto; $("auto-score").value = String(summary.auto_min_score);
  render();
}
async function act(done, fn) { try { await fn(); say(done); await load(); } catch (e) { say(e.message, true); } }

async function saveAuto() {
  await act($("auto").checked ? `Automatic orders on for score ${$("auto-score").value} and above.` : "Automatic orders off.",
    () => api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paper_auto: $("auto").checked, paper_auto_min_score: Number($("auto-score").value) }) }));
}
$("auto").onchange = saveAuto; $("auto-score").onchange = saveAuto;
$("reset").onclick = () => { if (confirm("Archive every paper order and trade and start again from the starting capital?")) act("Paper account reset.", () => post("/api/paper/reset")); };

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live`);
  ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.type === "prices") { Object.assign(prices, m.data); if (summary) render(); } else if (m.type === "paper" || m.type === "scan") load().catch(() => {}); };
  ws.onclose = () => setTimeout(connect, 3000);
}
showToken($("token"));
load().catch((e) => say(`Could not load paper trading. ${e.message}`, true));
connect();
