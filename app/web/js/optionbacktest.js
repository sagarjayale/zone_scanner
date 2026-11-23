// Option backtest page: choose a finished stock backtest, replay it with options, follow the progress, see the result.
const $ = (id) => document.getElementById(id);
const stamp = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "");
function say(text, isError = false) { $("status").textContent = text; $("status").className = "status" + (isError ? " err" : ""); }
const SRC = { auto: "Auto", real: "Real only", estimate: "Estimated" };
const WHY = { "stock exit": "Stock exit", expiry: "Expiry day" };
let shown = null, timer = null;

function runLabel(r) {
  const s = r.settings || {};
  const modes = (s.modes || []).map((m) => MODE_LABEL[m] || m).join(", ");
  const h = r.headline || {};
  return `#${r.id} · ${modes} · ${s.start || ""} to ${s.end || ""} · ${h.trades ?? "?"} trades`;
}
const kindText = (r) => ((r.settings || {}).kind === "scan" ? "check" : "range");

async function loadRuns() {
  const d = await api("/api/option-backtest/runs");
  const pick = $("stock-run"), keep = pick.value;
  const groups = [];
  if (d.stock_runs.length) groups.push(el("optgroup", { label: "Date-range backtests" }, ...d.stock_runs.map((r) => el("option", { value: r.id }, runLabel(r)))));
  if (d.scan_checks.length) groups.push(el("optgroup", { label: "Scan-date checks (Backtest page)" }, ...d.scan_checks.map((c) =>
    el("option", { value: c.id }, `#${c.id} · ${MODE_LABEL[c.mode] || c.mode} · scan date ${c.scan_date} · ${c.stocks} stocks · ${c.entered} entered`))));
  pick.replaceChildren(...(groups.length ? groups : [el("option", { value: "" }, "Nothing finished yet: use the form above, or Check this date on the Backtest page")]));
  if (keep && [...pick.options].some((o) => o.value === keep)) pick.value = keep;
  const tb = $("runs"); tb.replaceChildren();
  if (!d.runs.length) tb.append(el("tr", {}, el("td", { colspan: "9" }, "No option backtest yet.")));
  for (const r of d.runs) {
    const s = r.summary || {};
    tb.append(el("tr", {},
      el("td", {}, `#${r.id}`), el("td", {}, `${kindText(r) === "check" ? "check" : "backtest"} #${r.source_run_id}`), el("td", {}, SRC[(r.settings || {}).source] || ""),
      el("td", {}, r.status === "running" ? `running · ${r.progress || ""}` : r.status, r.error ? el("span", { class: "sub loss" }, r.error) : ""),
      el("td", { class: "num" }, s.trades == null ? "" : `${s.trades}${s.skipped ? ` (+${s.skipped} skipped)` : ""}`),
      el("td", { class: "num" }, s.win_rate == null ? "" : `${s.win_rate}%`),
      el("td", { class: "num " + ((s.total_pnl || 0) > 0 ? "win" : (s.total_pnl || 0) < 0 ? "loss" : "") }, s.total_pnl == null ? "" : signed(s.total_pnl)),
      el("td", { class: "num" }, s.total_r == null ? "" : `${s.total_r > 0 ? "+" : ""}${s.total_r} R`),
      el("td", {}, el("div", { class: "rowbtns" },
        el("button", { class: "btn", onclick: () => show(r.id) }, "Open"),
        r.status === "running" ? el("button", { class: "btn", onclick: () => post(`/api/option-backtest/${r.id}/cancel`).then(loadRuns) }, "Cancel")
          : el("button", { class: "btn", onclick: () => remove(r.id) }, "Delete")))));
  }
  const running = d.runs.some((r) => r.status === "running");
  clearTimeout(timer);
  if (running) timer = setTimeout(() => { loadRuns(); if (shown) show(shown, true); }, 2000);
}

async function remove(id) {
  if (!confirm(`Delete option backtest #${id}?`)) return;
  try { await api(`/api/option-backtest/${id}`, { method: "DELETE" }); if (shown === id) { shown = null; $("result").hidden = true; } loadRuns(); }
  catch (e) { say(e.message, true); }
}

async function show(id, quiet = false) {
  const d = await api(`/api/option-backtest/${id}`);
  shown = id;
  $("result").hidden = false;
  $("result-title").textContent = `Option backtest #${d.id} (from ${kindText(d) === "check" ? "scan-date check" : "stock backtest"} #${d.source_run_id})${d.status === "running" ? ` · ${d.progress || "running"}` : ""}`;
  $("export").href = `/api/option-backtest/${id}/export`;
  const s = d.summary || {};
  const fig = [["Trades", s.trades ?? d.trades.filter((t) => t.status === "done").length], ["Win rate", s.win_rate == null ? "" : `${s.win_rate}%`],
    ["Profit or loss after charges", s.total_pnl == null ? "" : `${s.total_pnl < 0 ? "−" : "+"}₹${money(Math.abs(s.total_pnl))}`], ["Charges", s.charges == null ? "" : `₹${money(s.charges)}`],
    ["Average R", s.avg_r ?? ""], ["Total R", s.total_r ?? ""], ["Profit factor", s.profit_factor ?? ""],
    ["Largest fall (drawdown)", s.max_drawdown == null ? "" : `₹${money(s.max_drawdown)}`], ["Prices", s.trades ? `${s.real || 0} real · ${s.estimate || 0} estimated` : ""]];
  $("figures").replaceChildren(...fig.map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, String(v)))));
  $("compare").textContent = s.stock_total_r == null ? "" : `The same trades as shares: ${s.stock_total_r > 0 ? "+" : ""}${s.stock_total_r} R in total, ` +
    `${s.stock_win_rate}% won. As options: ${s.total_r > 0 ? "+" : ""}${s.total_r} R, ${s.win_rate}% won.` + (s.real_prices ? ` Real prices: ${s.real_prices}.` : "");
  const tb = $("trades"); tb.replaceChildren();
  const done = d.trades.filter((t) => t.status === "done");
  if (!done.length) tb.append(el("tr", {}, el("td", { colspan: "13" }, d.status === "running" ? "Working…" : "No trade could be replayed.")));
  for (const t of done) {
    tb.append(el("tr", {},
      el("td", {}, el("strong", {}, t.symbol || ""), el("span", { class: "sub" }, MODE_LABEL[t.mode] || t.mode || "")),
      el("td", {}, el("span", { class: "tag " + (t.signal === "BUY" ? "demand" : "supply") }, t.signal === "BUY" ? "BUY → CE" : "SELL → PE")),
      el("td", {}, el("strong", {}, t.option_label), el("span", { class: "sub" }, `delta ${t.delta} · IV ${t.iv == null ? "" : Math.round(t.iv * 100) + "%"}`)),
      el("td", { class: "when" }, stamp(t.entry_ts)), el("td", { class: "when" }, stamp(t.exit_ts)),
      el("td", { class: "num" }, el("strong", {}, `${fmtPrice(t.premium_in)} → ${fmtPrice(t.premium_out)}`)),
      el("td", { class: "num" }, `${fmtPrice(t.stock_entry)} → ${fmtPrice(t.stock_exit)}`),
      el("td", {}, WHY[t.exit_reason] || t.exit_reason || ""),
      el("td", { class: "num" }, `${t.lots} × ${t.lot_size}`),
      el("td", { class: "num" }, fmtPrice(t.charges)),
      el("td", { class: "num " + (t.pnl_after > 0 ? "win" : "loss") }, el("strong", {}, signed(t.pnl_after)), el("span", { class: "sub" }, `before charges ${signed(t.pnl)}`)),
      el("td", { class: "num" }, `${t.result_r == null ? "" : (t.result_r > 0 ? "+" : "") + t.result_r} / ${t.stock_r == null ? "" : (t.stock_r > 0 ? "+" : "") + t.stock_r}`),
      el("td", {}, t.source === "real" ? "real" : el("span", { class: "sub" }, "estimated"))));
  }
  const skipped = d.trades.filter((t) => t.status === "skipped");
  $("skips").replaceChildren(...(skipped.length ? skipped.slice(0, 200).map((t) => el("li", {}, `${t.symbol || (t.instrument_key || "").split("|").pop()} ${stamp(t.entry_ts)}: ${t.note || ""}`))
    : [el("li", {}, "None.")]));
  if (!quiet) $("result").scrollIntoView({ behavior: "smooth", block: "start" });
}

$("run").onclick = async () => {
  const id = Number($("stock-run").value);
  if (!id) { say("Run a stock backtest first on the Backtest page.", true); return; }
  $("run").disabled = true;
  try {
    const r = await post("/api/option-backtest/run", { source_run_id: id, source: $("source").value });
    say(`Option backtest #${r.run_id} started.`);
    await loadRuns();
    show(r.run_id);
  } catch (e) { say(e.message, true); } finally { $("run").disabled = false; }
};

// New backtest with options: stocks, modes, dates, then one button.
const nBoxes = () => [...$("n-symbols").querySelectorAll("input")];
function syncAll() { $("n-all").checked = nBoxes().every((b) => b.checked); }
$("n-all").onchange = () => { for (const b of nBoxes()) b.checked = $("n-all").checked; };
const HISTORY_HELP = { short: "30 min, Hourly, 2 hr and 4 hr use 5-minute candles, kept for the last 6 months only.",
  long: "Daily and Weekly go back to 2022; Monthly and longer to 2000. Aim for at least 30 closed trades." };
$("n-run").onclick = async () => {
  const modes = [...$("n-modes").querySelectorAll("input:checked")].map((b) => b.value);
  const symbols = $("n-all").checked ? "all" : nBoxes().filter((b) => b.checked).map((b) => b.value);
  if (!modes.length) { say("Choose at least one mode.", true); return; }
  if (Array.isArray(symbols) && !symbols.length) { say("Choose at least one stock.", true); return; }
  $("n-run").disabled = true;
  try {
    const r = await post("/api/option-backtest/start", { symbols, modes, start: $("n-start").value, end: $("n-end").value, source: $("n-source").value });
    say(`Started: stock backtest #${r.stock_run_id}, then option backtest #${r.run_id}.`);
    await loadRuns();
    show(r.run_id);
  } catch (e) { say(e.message, true); } finally { $("n-run").disabled = false; }
};

(async function init() {
  showToken($("token"));
  try {
    const s = await api("/api/settings");
    const names = s.symbols || {}, stocks = s.ready || s.universe || [];
    for (const k of stocks) $("n-symbols").append(el("label", { class: "check" }, Object.assign(el("input", { type: "checkbox", value: k }), { checked: true }), names[k] || k));
    $("n-symbols").onchange = syncAll;
    for (const m of Object.keys(MODES)) $("n-modes").append(el("label", { class: "check" }, Object.assign(el("input", { type: "checkbox", value: m }), { checked: m === "DAILY" }), MODE_LABEL[m] || m));
    const today = new Date();
    $("n-end").value = new Date(today - 7 * 864e5).toISOString().slice(0, 10);
    $("n-start").value = new Date(today - 365 * 864e5).toISOString().slice(0, 10);
    $("n-help").textContent = `${HISTORY_HELP.short} ${HISTORY_HELP.long}`;
    await loadRuns();
    const want = Number(new URLSearchParams(location.search).get("show"));
    if (want) show(want);
  } catch (e) { say(`Could not load option backtests. ${e.message}`, true); }
})();
