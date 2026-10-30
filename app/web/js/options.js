// Options page: the live chain near the money, with a suggested strike when a zone is given in the link.
const $ = (id) => document.getElementById(id);
const q = new URLSearchParams(location.search);
let names = {}, zoneId = q.get("zone_id"), view = null;
function say(text, isError = false) { $("status").textContent = text; $("status").className = "status" + (isError ? " err" : ""); }
const num = (n, d = 2) => (n == null ? "" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: d }));

function leg(l, cells, dim) { return cells.map((v) => el("td", { class: (l && !l.liquid ? "thin " : "") + (dim ? "dim" : "") }, l ? v(l) : "")); }

// What the scanner says, as an option decision: which side, whether it is time, the levels as premiums, and the checks.
function renderIndication(v) {
  const i = v.indication, banner = $("banner"), cards = $("scores");
  banner.hidden = false; cards.hidden = false; banner.replaceChildren(); cards.replaceChildren();
  const trade = i.signal !== "NO_TRADE", buy = i.signal === "BUY";
  banner.className = "side-banner " + (trade ? (buy ? "buy" : "sell") : "none");
  banner.append(el("p", { class: "head" }, trade ? `Scanner: ${buy ? "buy" : "sell"} in ${MODE_LABEL[i.mode]} mode. Look at ${buy ? "calls" : "puts"}` : `Scanner: no trade in ${MODE_LABEL[i.mode]} mode`));
  banner.append(el("p", {}, trade ? `Curve ${lower(i.curve)}, ITF ${lower(i.itf_trend)}. Entry zone ${fmtPrice(i.zone.proximal)} to ${fmtPrice(i.zone.distal)} on ${TF_LABEL[i.zone.timeframe]}.` : (i.reason || "The rules give no side.")));
  const c = i.confirmation, STATUS = { wait: "Wait", "get ready": "Get ready", enter: "Enter", invalid: "Invalid" };
  const card = (k, val, note, cls) => cards.append(el("div", {}, el("dt", {}, k), el("dd", { class: cls || "" }, val), el("span", { class: "sub" }, note || "")));
  card("Trade score", trade ? `${i.scanner_score} of 8` : "none", trade ? `takes ${lower(ENTRY_TEXT[i.entry_type] || LABEL_TEXT[i.zone_type] || "")}` : "");
  card("Confirmation score", c && c.pct != null ? `${c.points} of ${c.max}` : "none", c && c.label ? c.label : "", c ? "conf-t-" + c.label : "");
  card("Entry status", i.status ? STATUS[i.status] : "none", i.status_note || "", i.status ? "status-" + i.status.replace(" ", "-") : "");
  const dec = $("decision"); dec.hidden = !trade;
  if (!trade) return;
  const lv = i.levels, box = $("levels"), s = v.suggestion;
  box.replaceChildren();
  if (s && s.buy) box.append(el("p", {}, `Suggested: the ${num(s.buy.strike, 0)} ${s.buy.type}, last price ${num(s.buy.ltp)}, delta ${s.buy.delta}${s.buy.liquid ? "" : " (thinly traded)"}.`));
  if (lv) {
    const t = el("table", { class: "mini" }, el("thead", {}, el("tr", {}, el("th", {}, "Level"), el("th", {}, "Underlying"), el("th", {}, "Premium, estimated"))));
    const tb = el("tbody");
    const early = i.entry_waiting && i.entry_type && i.entry_type !== "TYPE_1";      // entry types 2 and 3 have no price until their candles come
    for (const [name, u, pr, cls] of [[early ? "Target, from the proximal line" : "Target", lv.target, lv.premium_target, "win"], [early ? "Proximal line (the entry comes later)" : "Entry", lv.entry, lv.premium_entry, ""], ["Stop loss", lv.stop, lv.premium_stop, "loss"]])
      tb.append(el("tr", {}, el("td", {}, name), el("td", { class: "num" }, fmtPrice(u)), el("td", { class: "num " + cls }, `about ${num(pr)}`)));
    t.append(tb); box.append(t);
    if (early) box.append(el("p", { class: "foot" }, `${ENTRY_TEXT[i.entry_type]} waits until ${ENTRY_HOW[i.entry_type]}. The real entry, and so the target, will be a little different from the lines above.`));
    box.append(el("p", { class: "foot" }, `Lot size ${lv.lot_size}. Risk per lot ${money(lv.risk_per_lot)}, reward per lot ${money(lv.reward_per_lot)}${lv.rr == null ? "" : `, about ${lv.rr} to 1`}. ${lv.lots_for_risk == null ? "" : `${lv.lots_for_risk} ${lv.lots_for_risk === 1 ? "lot fits" : "lots fit"} your risk of ${money(lv.risk_budget)} per trade.`} Estimates use delta and gamma only; time decay and IV changes are not included.`));
  } else box.append(el("p", { class: "foot" }, "No premium estimate: the chain has no usable delta for this side right now."));
  const list = $("checks"); list.replaceChildren();
  for (const x of (c ? c.checks : [])) list.append(el("li", { class: !x.applicable ? "skip" : x.passed ? "pass" : x.points > 0 ? "part" : "fail" },
    el("span", { class: "mark" }, !x.applicable ? "not checked" : x.max > 1 ? `${x.points} of ${x.max}` : x.passed ? "yes" : "no"), el("span", {}, `${x.label}: ${x.note}`)));
}

function render() {
  const v = view, fig = $("figures");
  renderIndication(v);
  fig.hidden = false; fig.replaceChildren();
  const iv = v.iv, pct = iv.percentile == null ? `building history, ${iv.days} of ${iv.needed} days` : `${iv.percentile}%`;
  const figures = [["Spot", num(v.spot)], ["Put-call ratio", v.pcr ?? "none"], ["Call open interest", money(v.call_oi)], ["Put open interest", money(v.put_oi)],
    ["At-the-money IV", iv.atm_iv == null ? "no snapshot yet" : num(iv.atm_iv)], ["IV percentile", pct]];
  for (const [k, val] of figures) fig.append(el("div", {}, el("dt", {}, k), el("dd", { style: String(val).length > 12 ? "font-size:15px" : "" }, String(val))));
  const sug = $("suggestion"), s = v.suggestion;
  sug.hidden = !(s && s.sell); sug.replaceChildren();
  if (s && s.sell) sug.append(el("p", {}, `To sell a spread instead of buying: short the ${num(s.sell.strike, 0)} ${s.sell.type}, the first strike beyond the far side of the zone.`));
  const body = $("chain"), side = v.indication.side;
  $("chain-wrap").hidden = false; body.replaceChildren();
  if (!v.strikes.length) body.append(el("tr", {}, el("td", { colspan: "15" }, "Upstox returned an empty chain for this expiry.")));
  const paperBtn = (l, label) => (l ? el("button", { class: "btn", onclick: () => paperTrade(l, label) }, "Paper buy") : "");
  const tag = (l, wall, text, picked) => (l ? [picked ? el("span", { class: "tag demand" }, "Suggested") : "", wall ? el("span", { class: "tag approaching" }, text) : "", el("span", {}, l.buildup || "")] : []);
  for (const r of v.strikes) {
    const pick = s && s.buy && s.buy.strike === r.strike;
    const tr = el("tr", { class: (r.strike === v.atm ? "atm " : "") + (pick ? "pick" : "") });
    const label = (typ) => `${names[v.instrument_key] || v.instrument_key.split("|").pop()} ${num(r.strike, 0)} ${typ} ${v.expiry}`;
    const dc = side === "put", dp = side === "call";
    tr.append(el("td", { class: dc ? "dim" : "" }, paperBtn(r.call, label("CE"))),
      el("td", { class: "build " + (dc ? "dim" : "") }, ...tag(r.call, r.strike === v.call_wall, "Largest call OI: resistance", pick && s.buy.type === "call")),
      ...leg(r.call, [(l) => money(l.oi), (l) => signed(l.oi_change), (l) => num(l.iv), (l) => num(l.delta), (l) => num(l.ltp)], dc),
      el("td", { class: "strike" }, num(r.strike, 0), r.strike === v.atm ? el("span", { class: "sub" }, "at the money") : ""),
      ...leg(r.put, [(l) => num(l.ltp), (l) => num(l.delta), (l) => num(l.iv), (l) => signed(l.oi_change), (l) => money(l.oi)], dp),
      el("td", { class: "build " + (dp ? "dim" : "") }, ...tag(r.put, r.strike === v.put_wall, "Largest put OI: support", pick && s.buy.type === "put")),
      el("td", { class: dp ? "dim" : "" }, paperBtn(r.put, label("PE"))));
    body.append(tr);
  }
  say(`Chain as of ${String(v.as_of).replace("T", " ").slice(0, 19)}. Greyed options fail the liquidity settings. The dimmed side is the one the scanner is not pointing to.`);
}

async function paperTrade(l, label) {
  try { const t = await post("/api/paper/orders", { option_key: l.instrument_key, label, lots: 1 }); say(`Paper position opened: ${label}, ${t.quantity} at ${num(t.fill_price)}. See the Paper page.`); }
  catch (e) { say(`Could not open the paper position. ${e.message}`, true); }
}

async function loadChain() {
  const key = $("symbol").value;
  if (!key) return;
  $("refresh").disabled = true; setTimeout(() => { $("refresh").disabled = false; }, 5000);   // one request every few seconds at most
  try {
    say("Loading the chain");
    const p = new URLSearchParams();
    if ($("expiry").value) p.set("expiry", $("expiry").value);
    p.set("mode", $("mode").value);
    if (zoneId) p.set("zone_id", zoneId);
    view = await api(`/api/options/${encodeURIComponent(key)}?${p}`);
    render();
  } catch (e) { $("figures").hidden = true; $("chain-wrap").hidden = true; $("suggestion").hidden = true; $("banner").hidden = true; $("scores").hidden = true; $("decision").hidden = true; say(`Could not load the chain. ${e.message}`, true); }
}

async function loadExpiries() {
  const sel = $("expiry");
  sel.replaceChildren();
  try { for (const d of await api(`/api/options/${encodeURIComponent($("symbol").value)}/expiries`)) sel.append(el("option", { value: d }, d)); } catch (e) { /* shown by loadChain */ }
  await loadChain();
}

(async function init() {
  showToken($("token"));
  try {
    const s = await api("/api/settings");
    names = s.symbols || {};
    const keys = s.ready || s.universe || [];
    if (q.get("symbol") && !keys.includes(q.get("symbol"))) keys.unshift(q.get("symbol"));
    for (const k of keys) $("symbol").append(el("option", { value: k }, names[k] || k.split("|").pop()));
    if (q.get("symbol")) $("symbol").value = q.get("symbol");
    if (!keys.length) return say("No instruments in your list yet. Add them under Settings.");
    $("symbol").onchange = () => { zoneId = null; loadExpiries(); };
    for (const m of Object.keys(MODES)) $("mode").append(el("option", { value: m }, modeText(m)));
    $("mode").value = MODES[q.get("mode")] ? q.get("mode") : "MONTHLY";
    $("mode").onchange = loadChain;
    $("expiry").onchange = loadChain; $("refresh").onclick = loadChain;
    await loadExpiries();
  } catch (e) { say(`Could not load the page. ${e.message}`, true); }
})();