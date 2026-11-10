// Settings page: stock list, zone rules, paper and option settings, Telegram, jobs and the rejection log.
const $ = (id) => document.getElementById(id);
let S = {};
function say(text, isError = false) { $("status").textContent = text; $("status").className = "status" + (isError ? " err" : ""); window.scrollTo({ top: 0 }); }
const put = (body) => api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const stamp = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "");
const RULES = [["base_body_pct", "Base candle: body below, % of range", 1], ["leg_body_pct", "Explosive candle: body above, % of range", 1],
  ["max_base", "Most base candles", 1], ["gap_exciting_pct", "Gap candle counts as exciting (PDF marking only): opens beyond the previous high or low by at least, % of price (0 = off)", 0.05],
  ["max_wick_tests", "Tests allowed on an execution zone (1: a second test makes it unusable)", 1], ["min_score", "Smallest trade score shown (7 is entry type 1 only)", 0.5],
  ["min_htf_score", "Smallest score for an HTF location zone", 0.5], ["ema_period", "EMA for the extra point", 1], ["ema_period_2", "Second EMA for the extra point (PDF marking only)", 1],
  ["approach_pct", "Approaching: within, % of the proximal line", 0.1], ["ltf_max_gap_pct", "Ignore execution zones further than, % of price", 1],
  ["htf_buffer_pct", "Allow execution zones beyond the HTF distal by, % of its width", 1],
  ["contact_touch_pct", "HTF contact: touching within, % of price", 0.05], ["contact_near_pct", "HTF contact: near within, % of price", 0.1],
  ["target_rr", "Target, times the distance from entry to stop loss", 0.5]];
const MISC_NUM = ["paper_capital", "paper_risk_pct", "paper_slippage_pct", "option_max_spread_pct", "option_min_oi", "brokerage_per_order", "brokerage_pct",
  "stt_delivery_pct", "stt_intraday_pct", "exchange_txn_pct", "sebi_fee_per_crore", "gst_pct", "stamp_delivery_pct", "stamp_intraday_pct"];
const MISC_BOOL = ["fiscal_year", "ltf_alerts_all_zones"];

function renderUniverse() {
  const box = $("universe");
  box.replaceChildren();
  if (!(S.universe || []).length) box.append(el("span", { class: "empty" }, "The list is empty. Find an instrument below and add it."));
  for (const key of S.universe || []) {
    box.append(el("span", { class: "chip" }, S.symbols[key] || key, el("button", { "aria-label": `Remove ${S.symbols[key] || key}`, onclick: () => saveUniverse(S.universe.filter((k) => k !== key), `${S.symbols[key] || key} removed.`) }, "\u00d7")));
  }
}
async function saveUniverse(list, done) {
  try { S = await put({ universe: list }); renderUniverse(); say(S.sync_job_id ? `${done} It is being downloaded and scanned now; see Recent jobs.` : done); loadJobs(); }
  catch (e) { say(e.message, true); }
}
async function find() {
  const box = $("found"), text = $("find").value.trim();
  box.replaceChildren();
  if (text.length < 2) return box.append(el("span", { class: "empty" }, "Type at least two letters."));
  try {
    const rows = await api(`/api/instruments/search?q=${encodeURIComponent(text)}`);
    if (!rows.length) return box.append(el("span", { class: "empty" }, "Nothing found. If the instruments file was never downloaded, use Refresh instruments on the Data page."));
    for (const r of rows) {
      if ((S.universe || []).includes(r.instrument_key)) continue;
      box.append(el("button", { class: "chip add", title: r.name || "", onclick: () => { saveUniverse([...(S.universe || []), r.instrument_key], `${r.symbol} added.`); box.replaceChildren(); } }, `Add ${r.symbol}${r.segment === "NSE_INDEX" ? " (index)" : ""}`));
    }
  } catch (e) { say(e.message, true); }
}

// ---- add many at once: an index's members, or pasted symbols
function showPicker(res) {
  const list = $("picker-list"), missing = res.rows.filter((r) => !r.found), already = res.rows.filter((r) => r.in_list).length;
  $("picker").hidden = false;
  list.replaceChildren();
  for (const r of res.rows) {
    const box = el("input", { type: "checkbox", value: r.instrument_key || "" });
    box.checked = r.found && !r.in_list; box.disabled = !r.found || r.in_list;
    list.append(el("label", { class: "check", title: r.name }, box, r.symbol, el("span", { class: "hint" }, r.in_list ? "already in your list" : r.found ? r.industry : "not found in Upstox instruments")));
  }
  const parts = [`${res.index}: ${res.count} ${res.count === 1 ? "stock" : "stocks"}`];
  if (res.source) parts.push("read just now from niftyindices.com");
  if (already) parts.push(`${already} already in your list`);
  if (missing.length) parts.push(`${missing.length} not found (${missing.map((r) => r.symbol).join(", ")})`);
  $("picker-note").textContent = parts.join(", ") + ". Each new stock needs about 30 Upstox requests to download, so 50 stocks take close to an hour in the background.";
  countPicked();
}
function picked() { return [...$("picker-list").querySelectorAll("input:checked:not(:disabled)")].map((i) => i.value); }
function countPicked() { const n = picked().length; $("pick-add").textContent = n ? `Add selected (${n})` : "Add selected"; $("pick-add").disabled = !n; }
function setAll(on) { for (const i of $("picker-list").querySelectorAll("input:not(:disabled)")) i.checked = on; countPicked(); }
async function loadIndex() {
  try { say("Reading the index list from niftyindices.com"); showPicker(await api(`/api/instruments/index/${$("idx-name").value}`)); $("status").textContent = ""; }
  catch (e) { $("picker").hidden = true; say(e.message, true); }
}
async function matchPasted() {
  try { showPicker(await post("/api/instruments/match", { text: $("paste").value })); $("status").textContent = ""; }
  catch (e) { $("picker").hidden = true; say(e.message, true); }
}
async function addPicked() {
  const keys = picked();
  if (!keys.length) return;
  $("picker").hidden = true;
  await saveUniverse([...(S.universe || []), ...keys.filter((k) => !(S.universe || []).includes(k))], `${keys.length} ${keys.length === 1 ? "stock" : "stocks"} added.`);
}

function renderRules() {
  const scope = $("th-scope").value, over = scope ? (S[`thresholds:${scope}`] || {}) : null, form = $("th-form");
  form.replaceChildren();
  for (const [key, label, step] of RULES) {
    const input = el("input", { type: "number", id: "th-" + key, step: String(step), min: "0" });
    if (over) { if (over[key] != null) input.value = over[key]; input.placeholder = `${S[key]} (all timeframes)`; } else input.value = S[key];
    form.append(el("div", {}, el("label", { class: "lbl", for: "th-" + key }, label), input));
  }
  if (over) form.append(el("p", { class: "hint" }, "Leave a field empty to use the value for all timeframes."));
}
async function saveRules() {
  const scope = $("th-scope").value, out = {};
  for (const [key] of RULES) { const v = $("th-" + key).value; if (v !== "") out[key] = Number(v); }
  try { S = await put(scope ? { [`thresholds:${scope}`]: out } : out); renderRules(); say("Zone rules saved. They apply to zones found from now on; use Rebuild all zones to apply them to history."); }
  catch (e) { say(e.message, true); }
}

async function loadJobs() {
  const body = $("jobs"); body.replaceChildren();
  const jobs = await api("/api/jobs");
  if (!jobs.length) body.append(el("tr", {}, el("td", { colspan: "5" }, "No jobs have run yet.")));
  for (const j of jobs) body.append(el("tr", {}, el("td", {}, j.type), el("td", {}, stamp(j.started_at)), el("td", {}, stamp(j.ended_at)),
    el("td", { class: j.status === "failed" ? "loss" : "" }, j.status), el("td", { style: "white-space:normal" }, j.error || j.progress || "")));
}
async function loadRejections() {
  const body = $("rejections"); body.replaceChildren();
  const rows = await api("/api/rejections?limit=50");
  if (!rows.length) body.append(el("tr", {}, el("td", { colspan: "4" }, "No rejections logged in the last 90 days.")));
  for (const r of rows) body.append(el("tr", {}, el("td", {}, (S.symbols || {})[r.instrument_key] || r.instrument_key), el("td", {}, TF_LABEL[r.timeframe]), el("td", {}, stamp(r.ts)), el("td", {}, r.reason)));
}
async function follow(jobId, label) {
  for (;;) { const j = await api(`/api/jobs/${jobId}`); if (j.status === "running") { say(`${label}: ${j.progress || "starting"}`); await new Promise((r) => setTimeout(r, 1500)); continue; }
    say(j.status === "failed" ? `${label} failed. ${j.error}` : `${label} finished. ${j.progress || ""}`, j.status === "failed"); loadJobs(); return; }
}

function renderModes() {
  const box = $("modes"), on = S.modes || Object.keys(MODES);
  box.replaceChildren();
  for (const m of Object.keys(MODES)) box.append(el("label", { class: "check" }, Object.assign(el("input", { type: "checkbox", value: m }), { checked: on.includes(m) }), modeText(m)));
}

function fill() {
  renderUniverse(); renderRules(); renderModes();
  for (const k of MISC_NUM) $(k).value = S[k];
  for (const k of MISC_BOOL) $(k).checked = Boolean(S[k]);
  $("nse_product").value = S.nse_product === "intraday" ? "intraday" : "delivery";
  $("direction_rule").value = S.direction_rule || "location";
  $("entry_23").value = S.entry_23 === "TYPE_2" ? "TYPE_2" : "TYPE_3";
  $("marking_rule").value = S.marking_rule === "pdf" ? "pdf" : "scanner";
  $("zone_history").value = S.zone_history === "all" ? "all" : "scanner";
  $("htf_zone_pick").value = S.htf_zone_pick === "any" ? "any" : "fresh";
  $("ltf_min_bases").value = S.ltf_min_bases ?? 0; $("ltf_max_bases").value = S.ltf_max_bases ?? 0;
  $("min_stop_pct").value = S.min_stop_pct ?? 0; $("skip_reaction_zones").checked = Boolean(S.skip_reaction_zones);
  $("check_next_htf").checked = Boolean(S.check_next_htf);
  $("itf_rule").value = S.itf_rule === "atr" ? "atr" : "pct"; $("itf_slope_pct").value = S.itf_slope_pct ?? 0.05; $("itf_atr_mult").value = S.itf_atr_mult ?? 0.1;
  showItfFields();
  $("trail_method").value = S.trail_method || "lock"; $("trail_tf").value = ["ltf", "itf"].includes(S.trail_tf) ? S.trail_tf : "live";
  $("trail_lock_behind").checked = Boolean(S.trail_lock_behind);
  for (const k of TRAIL_NUM) $(k).value = S[k];
  $("trail_lock_target").value = S.trail_lock_target === "close" ? "close" : "move";
  $("trail_keep_target").checked = S.trail_keep_target !== false;
  showTrailFields(); showTrailExample();
  $("telegram_chat_id").value = S.telegram_chat_id || ""; $("telegram_alerts").checked = Boolean(S.telegram_alerts);
  $("tg-state").textContent = S.telegram_configured ? "A bot token and chat id are stored. Alerts and paper fills are sent to that chat." : "Not set up yet. Create a bot with BotFather in Telegram, then enter its token and your chat id.";
}

$("idx-load").onclick = loadIndex; $("paste-go").onclick = matchPasted; $("paste").onkeydown = (e) => { if (e.key === "Enter") matchPasted(); };
$("pick-all").onclick = () => setAll(true); $("pick-none").onclick = () => setAll(false); $("pick-add").onclick = addPicked;
$("picker-list").onchange = countPicked;
$("find-go").onclick = find; $("find").onkeydown = (e) => { if (e.key === "Enter") find(); };
$("th-scope").onchange = renderRules; $("th-save").onclick = saveRules;
$("rebuild").onclick = async () => { if (confirm("Clear every zone and plan and scan all stored candles again with the current rules? Candles and paper trades are kept.")) { try { follow((await post("/api/rebuild/zones")).job_id, "Rebuild"); } catch (e) { say(e.message, true); } } };
$("modes-save").onclick = async () => {
  const on = [...$("modes").querySelectorAll("input:checked")].map((i) => i.value);
  if (!on.length) return say("Keep at least one mode switched on.", true);
  try { S = await put({ modes: on }); fill(); say("Modes saved. They apply from the next scan."); } catch (e) { say(e.message, true); }
};
$("rule-save").onclick = async () => {
  try {
    S = await put({ direction_rule: $("direction_rule").value, itf_rule: $("itf_rule").value,
      itf_slope_pct: Number($("itf_slope_pct").value) || 0.05, itf_atr_mult: Number($("itf_atr_mult").value) || 0.1 }); fill();
    await post("/api/scan/run", { symbols: "all" });       // plans and results are rebuilt under the rule just saved
    say("Rule saved. Every stock is being rescanned now; see Recent jobs."); loadJobs();
  } catch (e) { say(e.message, true); }
};
$("marking-save").onclick = async () => {
  if (!confirm("Save the zone marking and history, and find every zone again with them? Candles and paper trades are kept; pending paper orders are cancelled if their zone is gone.")) return;
  try {
    S = await put({ marking_rule: $("marking_rule").value, zone_history: $("zone_history").value, htf_zone_pick: $("htf_zone_pick").value }); fill();
    follow((await post("/api/rebuild/zones")).job_id, "Rebuild");
  } catch (e) { say(e.message, true); }
};
$("entry-save").onclick = async () => {
  try {
    S = await put({ entry_23: $("entry_23").value, ltf_min_bases: Math.round(Number($("ltf_min_bases").value) || 0),
      ltf_max_bases: Math.round(Number($("ltf_max_bases").value) || 0), min_stop_pct: Number($("min_stop_pct").value) || 0,
      skip_reaction_zones: $("skip_reaction_zones").checked, check_next_htf: $("check_next_htf").checked }); fill();
    await post("/api/scan/run", { symbols: "all" });       // plans and results are rebuilt with the entry just chosen
    say("Entry saved. Every stock is being rescanned now; see Recent jobs."); loadJobs();
  } catch (e) { say(e.message, true); }
};
function showItfFields() {
  const atr = $("itf_rule").value === "atr";
  $("itf_slope_pct").closest("div").style.display = atr ? "none" : ""; $("itf_atr_mult").closest("div").style.display = atr ? "" : "none";
}
$("itf_rule").onchange = showItfFields;
const TRAIL_NUM = ["trail_start_r", "trail_atr_mult", "trail_swing", "trail_lock_pct", "trail_lock_gap_pct"];
// Only the fields of the chosen method are shown.
function showTrailFields() {
  const m = $("trail_method").value, show = (id, on) => { $(id).closest("div, label").style.display = on ? "" : "none"; };
  show("trail_tf", m !== "off"); show("trail_start_r", m !== "off" && m !== "lock"); show("trail_atr_mult", m === "atr"); show("trail_swing", m === "swing");
  show("trail_lock_pct", m === "lock"); show("trail_lock_gap_pct", m === "lock"); show("trail_lock_target", m === "lock"); show("trail_lock_behind", m === "lock");
  show("trail_keep_target", m !== "off" && m !== "lock");
}
$("trail_method").onchange = showTrailFields;
// The lock worked through on buy 100, stop 95, target 110, from the values in the form.
function showTrailExample() {
  const box = $("trail-example");
  if ($("trail_method").value !== "lock") { box.replaceChildren(); return; }
  box.replaceChildren(...lockExampleBox({ pct: $("trail_lock_pct").value, gapPct: $("trail_lock_gap_pct").value, behind: $("trail_lock_behind").checked,
    live: $("trail_tf").value === "live", move: $("trail_lock_target").value === "move" }));
}
for (const id of ["trail_method", "trail_tf", "trail_lock_pct", "trail_lock_gap_pct", "trail_lock_behind", "trail_lock_target"]) $(id).addEventListener("input", showTrailExample);
$("trail-defaults").onclick = async () => {
  try { S = await put({ ...TRAIL_DEFAULTS }); fill(); say("Trailing stop set to the defaults: lock, live price, step 10%, gap 20%. It applies to positions filled from now on; use Apply trailing on the Paper page for open ones."); }
  catch (e) { say(e.message, true); }
};
$("trail-save").onclick = async () => {
  const out = { trail_method: $("trail_method").value, trail_tf: $("trail_tf").value, trail_lock_target: $("trail_lock_target").value,
    trail_keep_target: $("trail_keep_target").checked, trail_lock_behind: $("trail_lock_behind").checked };
  for (const k of TRAIL_NUM) out[k] = Number($(k).value);
  try { S = await put(out); fill(); say(out.trail_method === "off" ? "Trailing stop switched off. Open positions keep the settings they were filled with." : "Trailing stop saved. It applies to positions filled from now on and to new checks on the Backtest page."); }
  catch (e) { say(e.message, true); }
};
$("misc-save").onclick = async () => { const out = { nse_product: $("nse_product").value }; for (const k of MISC_NUM) out[k] = Number($(k).value); for (const k of MISC_BOOL) out[k] = $(k).checked;
  try { S = await put(out); fill(); say("Settings saved."); } catch (e) { say(e.message, true); } };
$("tg-save").onclick = async () => { const out = { telegram_chat_id: $("telegram_chat_id").value.trim(), telegram_alerts: $("telegram_alerts").checked }; if ($("telegram_token").value) out.telegram_token = $("telegram_token").value.trim();
  try { S = await put(out); $("telegram_token").value = ""; fill(); say("Telegram settings saved."); } catch (e) { say(e.message, true); } };
$("tg-test").onclick = async () => { try { await post("/api/alerts/test"); say("Test alert sent. Check your Telegram chat."); } catch (e) { say(e.message, true); } };
$("check-token").onclick = async () => { try { const r = await api("/api/auth/status?check=true"); say(!r.has_token ? "No token stored. Log in with Upstox." : r.valid ? "The token works." : "The token is stored but Upstox rejected it. Log in again.", !(r.has_token && r.valid)); showToken($("token")); } catch (e) { say(e.message, true); } };
$("reconnect").onclick = async () => { try { await post("/api/feed/reconnect"); say("Live prices restarted."); } catch (e) { say(e.message, true); } };

(async function init() {
  showToken($("token"));
  for (const tf of TIMEFRAMES) $("th-scope").append(el("option", { value: tf }, `${TF_LABEL[tf]} only`));
  try {
    S = await api("/api/settings"); fill();
    const d = await api("/api/data/status"); $("calls").textContent = `Upstox requests used: ${d.api_calls["60s"]} in the last minute, ${d.api_calls["1800s"]} in the last 30 minutes (limit 1,000).`;
    await loadJobs(); await loadRejections();
  } catch (e) { say(`Could not load settings. ${e.message}`, true); }
})();