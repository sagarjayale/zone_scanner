// Premium page: low volatility, high volatility and stocks reacting from an HTF zone.
// Volatility is measured two ways: daily ATR % (every stock) or IV percentile (stocks with options and enough stored days).
(function () {
  const $ = (id) => document.getElementById(id);
  const KEY = "zoneScanner.premium";
  const DEFAULTS = { pick: "low_vol", mode: "DAILY", side: "both", measure: "atr" };
  let prefs = loadPrefs(KEY, DEFAULTS);
  if (!EYE_MODES.includes(prefs.mode)) prefs.mode = "DAILY";
  let data = null;

  const list = $("p-list");
  const pressList = () => list.querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.v === prefs.pick)));
  list.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    prefs.pick = b.dataset.v; pressList(); savePrefs(KEY, prefs); render();
  });

  for (const m of EYE_MODES) $("f-mode").append(el("option", { value: m }, `${MODE_LABEL[m]} (${TF_LABEL[MODES[m][0]]} zones)`));
  $("f-mode").value = prefs.mode;
  $("f-mode").onchange = () => { prefs.mode = $("f-mode").value; savePrefs(KEY, prefs); data = null; render(); loader.load(prefs.mode); };
  $("f-side").value = prefs.side;
  $("f-side").onchange = () => { prefs.side = $("f-side").value; savePrefs(KEY, prefs); render(); };
  $("f-measure").value = prefs.measure;
  $("f-measure").onchange = () => { prefs.measure = $("f-measure").value; savePrefs(KEY, prefs); render(); };
  buildGroupSelect($("f-group"));
  $("f-group").onchange = () => { saveGroup($("f-group").value); render(); };

  // the zone to show on a card: the side asked for, else the one nearer the price
  function zoneOf(r) {
    const sides = prefs.side === "both" ? ["demand", "supply"] : [prefs.side];
    const zs = sides.map((s) => r[s]).filter(Boolean);
    return zs.sort((a, b) => a.distance_pct - b.distance_pct)[0] || null;
  }

  function volText(r) {
    return prefs.measure === "iv" ? `IV ${r.atm_iv}, ${r.iv_pct} percentile` : `ATR ${r.atr_pct}% a day`;
  }

  function render() {
    const cards = $("cards");
    $("f-measure").disabled = prefs.pick === "reacting";
    if (!data) { $("s-line").textContent = `Loading ${MODE_LABEL[prefs.mode]} zones`; cards.replaceChildren(); return; }
    const rows = inGroup(data, $("f-group").value);
    let shown = [], line = "";
    if (prefs.pick === "reacting") {
      for (const r of rows) {
        const zs = (prefs.side === "both" ? ["demand", "supply"] : [prefs.side]).map((s) => r[s]).filter((z) => z && z.state === "reacting");
        if (zs.length) shown.push([r, zs[0], `reacting ${zs[0].side === "demand" ? "up" : "down"}, ${zs[0].distance_pct}% away (${zs[0].reaction_r}× zone)`]);
      }
      shown.sort((a, b) => a[1].distance_pct - b[1].distance_pct);
      line = `${shown.length} of ${rows.length} stocks reacting from a ${TF_LABEL[data.frames[0]].toLowerCase()} zone`;
    } else {
      const field = prefs.measure === "iv" ? "iv_pct" : "atr_pct";
      const measured = rows.filter((r) => r[field] != null).sort((a, b) => a[field] - b[field]);
      const third = Math.max(1, Math.round(measured.length / 3));
      const pick = !measured.length ? [] : prefs.pick === "low_vol" ? measured.slice(0, third) : measured.slice(-third).reverse();
      shown = pick.map((r) => [r, zoneOf(r), volText(r)]);
      const by = prefs.measure === "iv" ? "IV percentile" : "daily ATR %";
      line = `${shown.length} of ${measured.length} stocks: the ${prefs.pick === "low_vol" ? "lowest" : "highest"} third by ${by}`;
      if (prefs.measure === "iv" && measured.length < rows.length) line += ` (${rows.length - measured.length} without options or with too few stored days are left out)`;
    }
    cards.replaceChildren(...shown.map(([r, z, extra]) => stockCard(r, prefs.mode, z, extra)));
    if (!shown.length) cards.append(el("p", { class: "lede" }, "No stock matches right now."));
    $("s-line").textContent = line + ". Click a stock to open its chart." + waitingText(data);
    $("help").textContent = prefs.pick === "reacting"
      ? `Reacting: one of the last ${data.react_candles} closed ${TF_LABEL[data.frames[0]].toLowerCase()} candles touched an active zone and the price is now back outside it, moving away (above a demand zone, below a supply zone), not yet more than ${data.react_max_r} zone heights away. Only zones tested at most ${data.max_tests}× before, as for trading.`
      : prefs.measure === "iv"
        ? "IV percentile: the share of the last 252 stored days on which the at-the-money IV was lower than today. It comes from the option snapshots the Options page stores, so a stock needs options and enough stored days. Each card also shows the nearest zone of the mode."
        : "ATR %: the average true range of the last 14 daily candles as a % of the price. Every stock has it. Each card also shows the nearest zone of the mode.";
  }

  const loader = eyeLoader((d) => { if (d.mode === prefs.mode) { data = d; render(); } },
    (e) => { $("s-line").textContent = `Could not load the zones: ${e.message}`; });
  pressList(); render();
  loader.load(prefs.mode);
  loader.every(() => prefs.mode);
  showToken($("token"));
  setInterval(() => showToken($("token")), 30000);
})();
