// Shared helpers. Every value shown comes from the API; nothing is filled in here.
const TIMEFRAMES = ["5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w", "1mo", "3mo", "6mo", "1y"];
const TF_LABEL = { "5m": "5 min", "15m": "15 min", "30m": "30 min", "1h": "1 hr", "2h": "2 hr", "4h": "4 hr", "1d": "Day", "1w": "Week", "1mo": "Month", "3mo": "Quarter", "6mo": "Half year", "1y": "Year" };
// GTF analysis modes: HTF for location and curve, ITF for trend, LTF for the execution zone.
const MODES = { "30M": ["30m", "15m", "5m"], HOURLY: ["1h", "30m", "5m"], "2HR": ["2h", "30m", "5m"], "4HR": ["4h", "1h", "15m"], DAILY: ["1d", "1h", "30m"],
  WEEKLY: ["1w", "1d", "1h"], MONTHLY: ["1mo", "1w", "1d"], QUARTERLY: ["3mo", "1mo", "1d"], HALFYEARLY: ["6mo", "3mo", "1w"], YEARLY: ["1y", "6mo", "1mo"] };
const MODE_LABEL = { "30M": "30 min", HOURLY: "Hourly", "2HR": "2 hr", "4HR": "4 hr", DAILY: "Daily", WEEKLY: "Weekly", MONTHLY: "Monthly", QUARTERLY: "Quarterly", HALFYEARLY: "Half yearly", YEARLY: "Yearly" };
const modeText = (m) => (MODES[m] ? `${MODE_LABEL[m]} (${MODES[m].map((t) => TF_LABEL[t]).join(", ")})` : m || "");
const LABEL_TEXT = { TYPE_1: "Type 1", TYPE_2_3: "Type 2 or 3", WEAK: "Weak", REJECT: "below 5, no trade" };
// The three GTF entry types. A trade score of 7 or more takes type 1; 5 to 6.5 takes type 2 or 3.
const ENTRY_TEXT = { TYPE_1: "Entry type 1", TYPE_2: "Entry type 2", TYPE_3: "Entry type 3" };
const ENTRY_KEYS = [["type1", "Entry type 1"], ["type2", "Entry type 2"], ["type3", "Entry type 3"]];
const ENTRY_HOW = { TYPE_1: "limit at the proximal line", TYPE_2: "a candle closes inside the zone and the next opens inside it", TYPE_3: "a candle closes inside the zone and the next closes back out of it" };
// Names of the entries in a scan-date check. Checks saved before the entry types were added still carry the earlier three names.
const PLAN_NAME = { type1: "Entry type 1", type2: "Entry type 2", type3: "Entry type 3", aggressive: "Aggressive", confirmation: "Confirmation", conservative: "Conservative" };
const PLAN_SHORT = { type1: "Type 1", type2: "Type 2", type3: "Type 3", aggressive: "Aggr.", confirmation: "Conf.", conservative: "Cons." };
// Which candle gave the distal line. The leg-in or leg-out wick is the course's "exceptional marking".
const DISTAL_TEXT = { base: "the base candles", "leg-in": "the leg-in wick (exceptional marking)", "leg-out": "the leg-out wick (exceptional marking)" };
const testsText = (z) => (z.wick_tests ? `tested ${z.wick_tests === 1 ? "once" : z.wick_tests + " times"}` : "not tested yet") + (z.testing ? ", being tested now" : "");
const lower = (s) => (s ? String(s).toLowerCase().replace(/_/g, " ") : "unknown");

async function api(path, options) {
  const res = await fetch(path, options);
  let body = null;
  try { body = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) throw new Error((body && body.detail) || `${res.status} ${res.statusText}`);
  return body;
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });

// Exchange wall time kept as-is on the chart axis: treat the stored IST time as UTC.
const toEpoch = (s) => Date.parse(String(s).replace(" ", "T") + "Z") / 1000;
const fmtTime = (epoch, withClock = true) => {
  const d = new Date(epoch * 1000);
  const p = (n) => String(n).padStart(2, "0");
  const day = `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`;
  return withClock ? `${day} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}` : day;
};
const fmtPrice = (n) => (n == null ? "" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 }));
const el = (tag, attrs = {}, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v; else if (k.startsWith("on")) node.addEventListener(k.slice(2), v); else node.setAttribute(k, v);
  }
  for (const kid of kids) node.append(kid);
  return node;
};

async function showToken(badge) {
  try {
    const s = await api("/api/auth/status");
    badge.textContent = s.has_token ? "Upstox token stored" : "Not logged in to Upstox";
    badge.className = "badge " + (s.has_token ? "ok" : "bad");
  } catch (e) { badge.textContent = "Server not reachable"; badge.className = "badge bad"; }
}

// One navigation for every page.
const PAGES = [["scanner", "Dashboard"], ["chart", "Chart"], ["paper", "Paper"], ["backtest", "Backtest"], ["options", "Options"], ["data", "Data"], ["settings", "Settings"]];
(function buildNav() {
  const nav = document.getElementById("nav");
  if (!nav) return;
  for (const [page, label] of PAGES) {
    const a = el("a", { href: "/" + page }, label);
    if (page === nav.dataset.current) a.setAttribute("aria-current", "page");
    nav.append(a);
  }
})();
const money = (n) => (n == null ? "" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: 0 }));
const signed = (n) => (n == null ? "" : `${n > 0 ? "+" : ""}${money(n)}`);

// HTF contact: one phrase per higher timeframe, such as "Touches Week at 772".
function contactPhrase(c) {
  const tf = TF_LABEL[c.tf] || c.tf;
  if (c.relation === "inside") return `Inside ${tf}`;
  if (c.relation === "overlap") return `Overlaps ${tf} at ${fmtPrice(c.high)} to ${fmtPrice(c.low)}`;
  if (c.relation === "touch") return `Touches ${tf} at ${fmtPrice(c.at)}`;
  return `${tf} ${c.pct}% ${c.where}`;
}
const contactText = (list) => ((list || []).length ? list.map(contactPhrase).join(". ") + "." : "No higher timeframe is close");

// Score filters used by the dashboard and the backtest report: HTF demand, HTF supply, LTF demand zone, LTF supply zone.
// Each starts at 7 or more. The choices are remembered in this browser.
const SCORE_FILTERS = [["hd", "HTF demand score", [0, 4, 5, 5.5, 6, 7]], ["hs", "HTF supply score", [0, 4, 5, 5.5, 6, 7]],
  ["ld", "LTF demand zone score", [0, 5, 5.5, 6, 7, 8]], ["ls", "LTF supply zone score", [0, 5, 5.5, 6, 7, 8]]];
const SCORE_KEY = "zoneScanner.scoreFilters";
function scoreFilterValues() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(SCORE_KEY) || "{}"); } catch (e) { /* storage not available */ }
  const out = {};
  for (const [key] of SCORE_FILTERS) out[key] = saved[key] == null ? 7 : Number(saved[key]);
  return out;
}
// Build the four selects inside `box` and call `changed()` whenever one of them changes.
function buildScoreFilters(box, changed) {
  const now = scoreFilterValues();
  box.replaceChildren();
  for (const [key, label, steps] of SCORE_FILTERS) {
    const sel = el("select", { id: "sf-" + key, "aria-label": label });
    for (const v of steps) sel.append(el("option", { value: String(v) }, v ? `${label}: ${v}${v === steps[steps.length - 1] ? "" : " or more"}` : `${label}: any`));
    sel.value = String(now[key]);
    sel.onchange = () => { saveScoreFilters(); changed(); };
    box.append(sel);
  }
  box.append(el("button", { class: "btn", id: "sf-clear", onclick: () => { for (const [key] of SCORE_FILTERS) document.getElementById("sf-" + key).value = "0"; saveScoreFilters(); changed(); } }, "Clear score filters"));
}
function readScoreFilters() { const out = {}; for (const [key] of SCORE_FILTERS) out[key] = Number(document.getElementById("sf-" + key).value); return out; }
function saveScoreFilters() { try { localStorage.setItem(SCORE_KEY, JSON.stringify(readScoreFilters())); } catch (e) { /* storage not available */ } }
// Does one result row pass? A missing zone fails its filter. The LTF demand filter judges buy setups and the LTF
// supply filter judges sell setups; with either set, a row without an LTF zone is hidden.
function passesScores(r, f) {
  if (f.hd && !(r.demand_zone && r.demand_zone.score >= f.hd)) return false;
  if (f.hs && !(r.supply_zone && r.supply_zone.score >= f.hs)) return false;
  const z = r.ltf_zone;
  if ((f.ld || f.ls) && !z) return false;
  if (z && z.side === "demand" && f.ld && z.score < f.ld) return false;
  if (z && z.side === "supply" && f.ls && z.score < f.ls) return false;
  return true;
}