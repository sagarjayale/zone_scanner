// Shared by the GTF EYE and Premium pages: the zone modes, page preferences, loading /api/eye, the stock groups and the stock cards.
const EYE_MODES = ["30M", "HOURLY", "2HR", "4HR", "DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "HALFYEARLY", "YEARLY"];
const EYE_REFRESH_MS = 30000;          // prices move: the cards are worked out again every 30 seconds (one quick read)
// "N stocks are still being prepared": stocks scanned before the EYE rows existed, saved in the background.
const waitingText = (d) => (d && d.waiting ? ` ${d.waiting} more ${d.waiting === 1 ? "stock is" : "stocks are"} still being prepared and will appear shortly.` : "");
const STATE_TEXT = { in_zone: "In zone", reacting: "Reacting", approaching: "Approaching" };

function loadPrefs(key, defaults) {
  try { return { ...defaults, ...(JSON.parse(localStorage.getItem(key) || "{}")) }; } catch (e) { return { ...defaults }; }
}
function savePrefs(key, prefs) { try { localStorage.setItem(key, JSON.stringify(prefs)); } catch (e) { /* storage not available */ } }

// Loads /api/eye for a mode. A load still running is never doubled; a later mode wins over an earlier one.
function eyeLoader(onData, onError) {
  let busy = false, wanted = null, timer = null;
  async function load(mode) {
    wanted = mode;
    if (busy) return;
    busy = true;
    try {
      while (wanted) {
        const m = wanted; wanted = null;
        const data = await api(`/api/eye?mode=${encodeURIComponent(m)}`);
        if (!wanted) onData(data);
        if (data.waiting) setTimeout(() => { if (!wanted) load(m); }, 5000);   // stocks still being prepared: look again soon
      }
    } catch (e) { onError(e); } finally { busy = false; }
  }
  return {
    load,
    every(modeFn) { clearInterval(timer); timer = setInterval(() => { if (!document.hidden) load(modeFn()); }, EYE_REFRESH_MS); },
  };
}

// Which stocks to show: all, stocks only, or indices only. Remembered per browser.
const GROUPS = [["all", "All"], ["stocks", "Stocks"], ["indices", "Indices"]];
const GROUP_KEY = "zoneScanner.eyeGroup";
function loadGroup() { try { return localStorage.getItem(GROUP_KEY) || "all"; } catch (e) { return "all"; } }
function saveGroup(g) { try { localStorage.setItem(GROUP_KEY, g); } catch (e) { /* storage not available */ } }
function buildGroupSelect(sel) { sel.replaceChildren(...GROUPS.map(([k, label]) => el("option", { value: k }, label))); sel.value = loadGroup(); if (!sel.value) sel.value = "all"; }

function chartLink(r, mode, zoneId) {
  const q = new URLSearchParams({ symbol: r.instrument_key, mode, tf: MODES[mode][0] });
  if (zoneId) q.set("zone", zoneId);
  return "/chart?" + q.toString();
}

// One stock card. z: the zone view of one side (or null), extra: a line under the price.
function stockCard(r, mode, z, extra) {
  const side = z ? z.side : "";
  const lines = [el("span", { class: "eye-name" }, r.symbol), el("span", { class: "eye-price" }, fmtPrice(r.price))];
  if (extra) lines.push(el("span", { class: "eye-extra" }, extra));
  if (z) {
    lines.push(el("span", { class: "eye-zone" }, `${z.pattern ? z.pattern + " " : ""}${fmtPrice(z.proximal)} – ${fmtPrice(z.distal)}`));
    lines.push(el("span", { class: "eye-score" }, `score ${z.score}` + (z.touches ? `, tested ${z.touches}×` : ", fresh")));
  }
  const title = z ? `${r.symbol}: ${side} zone ${fmtPrice(z.proximal)} to ${fmtPrice(z.distal)}, ${STATE_TEXT[z.state] || "away from it"}. Open the chart.`
                  : `${r.symbol}: open the chart.`;
  return el("a", { class: `eye-card ${side}`, href: chartLink(r, mode, z && z.zone_id), title }, ...lines);
}

// The stocks of the chosen group, in name order, as row objects.
function inGroup(data, group) {
  return data.rows.filter((r) => group === "stocks" ? !r.is_index : group === "indices" ? r.is_index : true)
    .sort((a, b) => a.symbol.localeCompare(b.symbol));
}
