# Zone scanner

Demand and supply zone scanner on Upstox data, using GTF rules: the HTF gives location and the curve, the ITF
gives the trend, and the LTF gives the execution zone. It includes data sync, chart with replay, backtest, live
scanner, paper trading, options, Telegram alerts and settings. Database: MySQL. No real order is ever sent to Upstox.

## Set up on Linux

```
sudo apt install python3-venv            # once, if venv is missing
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                     # then edit .env
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Run it as one process, as shown: the schedule and the price poller live inside the app, so do not add
`--workers`. Leave it running through the trading day.

In `.env`, fill in the Upstox key and secret and the MySQL values:

```
DB_HOST=localhost
DB_PORT=3306
DB_USER=your_mysql_user
DB_PASSWORD=your_password        # written as it is; @ and other symbols need no escaping
DB_NAME=scanner_db_adv
```

The database and its 14 tables are created on first start if they do not exist. The MySQL user needs
the right to create that database, or create it yourself first:
`CREATE DATABASE scanner_db_adv CHARACTER SET utf8mb4;`

Leave the `DB_` values empty to use a local SQLite file instead. Moving from SQLite to MySQL starts with an
empty database: press Sync all, then Scan all, on the Data page.

Open http://127.0.0.1:8000 for the pages, or http://127.0.0.1:8000/docs to call every endpoint by hand.

## First use, in order

1. Data page: Log in with Upstox. The redirect URL in your Upstox app must match `UPSTOX_REDIRECT_URI`.
2. Data page: Refresh instruments.
3. Settings page: build your stock list. Three ways: find one instrument by symbol; choose an index (Nifty 50,
   Nifty Bank, Nifty Next 50, Nifty Midcap 100) and tick the stocks you want; or paste several symbols at once.
   Each new stock is downloaded (about 30 Upstox requests) and scanned in the background, so all 50 Nifty stocks
   take close to an hour. The default list is the Nifty 50 and Nifty Bank indices themselves.
4. Data page: Sync all, then Scan all, if you want everything refreshed at once.
5. Scanner page: setups appear here. Chart, Paper trade and Options are on each row.

## Score filters

The dashboard and the backtest report share four score filters. Each starts at 7 or more.

| Filter | Judges | Highest score |
| --- | --- | --- |
| HTF demand score | The nearest HTF demand zone | 7 |
| HTF supply score | The nearest HTF supply zone | 7 |
| LTF demand zone score | The LTF entry zone of a buy setup | 8 (with the EMA point) |
| LTF supply zone score | The LTF entry zone of a sell setup | 8 (with the EMA point) |

- Every filter that is set must pass. A stock with no zone of that kind fails that filter.
- With either LTF filter set, a stock with no LTF entry zone is hidden, so "no trade" rows only show when both are on "any".
- "Clear score filters" sets all four to "any". A line above the table says how many stocks pass and how many are hidden.
- The choices are remembered in the browser and shared by both pages.
- On the backtest report the totals per plan are recalculated for the stocks that pass. A saved check keeps every
  stock, so you can reopen it and try other filters without running it again. The report also shows the HTF demand
  and supply zones with their scores.
- On the chart, every drawn zone carries its score and type, and the result panel shows the breakdown (freshness,
  strength, base, and the EMA point for the LTF zone). The "Zone scores" switch hides the labels.

## Confirmation score

A second score beside the scanner score, made of checks the zone rules do not make. The scanner score is unchanged.

| Check | Points | Passes when | Needs |
| --- | --- | --- | --- |
| Mode agreement | 2 | Every higher mode that has a side agrees (1 point when at least half do) | Stored results |
| HTF contact | 1 | The entry zone is inside, overlaps or touches a higher-timeframe zone | Stored zones |
| Index agreement | 1 | Nifty 50 gives the same side in the same mode | Nifty 50 in the stock list |
| Leg-out volume | 1 | The candle that formed the zone had 1.5 times the average volume of the 20 before it | Volume (not for indices) |
| Relative strength | 1 | The stock did better than Nifty 50 over the last 20 LTF candles for a buy, worse for a sell | Nifty 50 candles |
| Strike build-up | 1 | The suggested option's price is up since the previous close (long build-up or short covering) | Live chain |
| Open-interest wall | 1 | The strike with the most call OI (buy) or put OI (sell) is not between the entry and the target | Live chain |
| Put-call ratio | 1 | 1 or more for a buy, 1 or less for a sell | Live chain |
| IV level | 1 | IV percentile under 70 | 20 days of snapshots |
| Time to expiry | 1 | At least 5 days left | An expiry |
| Premium reward to risk | 1 | The estimated premiums give at least 1.5 to 1 | Live chain |

- A check that cannot be made is left out of both the points and the maximum, so the score always reads "points of
  what could be checked". 75% or more is strong, 50% or more moderate, less is weak.
- **Dashboard:** a Confirmation column from the first five checks (they need only stored data), the list of checks
  in the row details, and a filter: any, moderate or better, strong. It starts on "any" and is remembered.
- **Options page:** all eleven checks, since the live chain is loaded there.
- The put-call ratio check uses the level at the moment the chain is loaded, not its trend through the day.

## Options page

- **Mode** chooses which of the scanner's results is used. Arriving from a dashboard row sets it.
- **Banner:** the scanner's signal as an option side. Buy points to calls, sell to puts, and no trade shows the reason.
- **Cards:** scanner score, confirmation score, and entry status. Wait: more than 1% from the entry zone. Get ready:
  within 1%. Enter: price is at the zone. Invalid: price is beyond the stop loss.
- **Levels:** the plan's entry, stop loss and target with the estimated premium of the suggested option at each,
  the risk and reward per lot, and how many lots fit your paper risk per trade. The estimate moves the premium by
  delta for each point, with the gamma correction; time decay and IV changes are not included.
- **Chain:** the side the scanner points to is in full colour and the other side is dimmed. The suggested strike,
  the at-the-money strike, the largest call OI (resistance) and the largest put OI (support) are marked, and every
  option shows its build-up since the previous close.
- Option data is live only during market hours with a valid Upstox token.

## Stocks become available one by one

Every stock in the list goes through the same steps in the background: waiting, downloading, scanning, ready.

- A stock appears on the dashboard, chart, backtest and options pages as soon as its own download and scan are done.
  The others carry on; nothing waits for the whole list.
- Up to four stocks download side by side. Scans run one at a time in a worker thread, so the pages keep answering.
- A stock that has been ready once stays visible while it is being updated, and also if a later update fails; the
  failure and its reason are shown on the Data page.
- The Data page shows where every stock stands and refreshes itself while work is in progress.
- "Download and scan all" on the Data page starts the work for the whole list. Adding a stock on the Settings page
  starts it for that stock.

## The result of a mode

For each ready stock and each mode the app saves one result at every scan, shaped like the scan result of the
original scanner: price, nearest HTF demand and supply with the freshness, strength and base scores, market state,
curve, HTF trend, ITF trend with its reason, trade side, the best LTF entry zone with its score (including the EMA
bonus) and type, its state, the signal, the primary entry with stop loss and target, and all three entry plans.

- An execution zone may have been tested once before; a zone tested twice is used up.
- A zone the price is inside right now still counts as the entry zone, so "touching now" shows with live prices.
- The trade score picks the entry: 7 or more takes entry type 1, 5 to 6.5 takes entry type 2 or 3 (your choice in
  Settings), below 5 is no trade with the reason "The trade score is below 5".
- Entry types 2 and 3 have no price until their candles come. Until then the result shows the proximal line as the
  level to watch (`signal.watch`), the stop loss, and `waiting: true`; `signal.entry` and `signal.target` are empty.
- With live prices the dashboard works out the state itself; between scans the rest of the result is as of the last scan.

## Live mode

Once the app is running and you are logged in to Upstox, it works on its own on trading days (IST):

| When | What runs |
| --- | --- |
| 08:45 | Token check and instruments refresh |
| 09:00 | Catch-up sync and scan for every instrument in your list |
| 09:21 to 15:31, every 5 minutes | Bar close: fetch the newest 5 minute candle (and 30 minute at :16 and :46), rebuild 15 min, 1 hr, 2 hr and 4 hr, scan |
| 09:15 to 15:30, every 5 seconds | Live prices for your list, alerts when price comes within 1% of a setup's proximal line or reaches it |
| 15:40 | End of day sync and scan, including the daily candle and any week, month, quarter or year that closed |
| 09:23 to 15:28, every 5 minutes | Option chain snapshots |
| 16:00 | Gap repair for instruments with short days; old log rows cleared |

- Each job is recorded in the jobs table. A failed job (for example, an expired token) is recorded and shown; it never stops the app.
- Each alert fires once per zone: approaching, then entry reached. Alerts are stored in the events table, pushed to the scanner page and sent to Telegram.
- The same price poll fills paper orders and checks paper stops and targets.
- Log in to Upstox each morning before 08:45: the token expires daily.
- The schedule does not know exchange holidays. On a holiday the jobs run and find nothing new.
- Capacity: the bar close job uses about 7 Upstox requests per instrument per 30 minutes, so the limit of 1,000 per
  30 minutes allows roughly 130 instruments. The throttle keeps you inside the limit; a longer list simply takes longer per round.
- Live prices come from the Upstox price quote API polled every 5 seconds, not from the WebSocket feed. That was
  the simpler and more testable choice; the WebSocket feed can replace it later without changing anything else.
- Set `SCHEDULER_ENABLED=0` in `.env` to run the pages and APIs with no schedule and no price polling.

## Upgrading from an earlier version

Replace the code, delete the files listed below if they exist, and restart.

```
rm -f app/engine/topdown.py app/engine/indicators.py tests/test_z_topdown_grade.py
```

On the first start after an update that changes the rules (GTF-2 brought the trade score and entry types 1, 2 and 3;
GTF-3 the course marking; GTF-4 zones found, tested and scored as scanner.py v6.4 does; the rules version is now GTF-5: the
nearest HTF zones must never have been tested) the app:

- adds the new columns to your existing database;
- clears every zone, plan, zone event, rejection and saved mode result made by the earlier rules (candles, paper
  trades, saved scan-date checks and option snapshots are kept; pending paper orders are cancelled);
- removes the settings the rules no longer have (strong leg-out body and conservative target), and sets "Zone
  marking" back to its default, scanner.py v6.4;
- finds every zone again from the stored candles, in the background, as the job "rebuild after a rule change".
  No download is needed and the pages stay usable; each stock's result comes back as its rebuild finishes.

Saved scan-date checks made before the update keep their aggressive, confirmation and conservative columns and open
under those names. Run the date again to see it with the entry types.

## GTF rules as built

**Zone marking: two choices (Settings, "Zone marking").** The default is **scanner.py v6.4**: zones are found, tested,
used up and scored exactly as the user's scanner.py does. A test runs scanner.py's own zone functions
(tests/reference_scanner_v64.py, copied unchanged) next to the app's engine on 24 series of random candles and
checks that both find the same 201 HTF zones and 58 LTF zones, with the same lines, tests and scores. The other choice
is the **GTF course PDF** marking, described after this list.

scanner.py v6.4 marking:

- Base candle: body below 50% of the range. Leg-out: body above 50%, green for demand, red for supply.
- The leg-in is the candle before the base and can be any candle on the HTF. 1 to 6 base candles. A longer run of base
  candles is cut as scanner.py walks it, in blocks of six with one candle between: 7 base candles give no zone,
  8 give a base of 1.
- The leg-out must close beyond the leg-in's close (above it for demand, below it for supply).
- Lines from the base candles only: demand proximal = highest base body, distal = lowest base wick (supply mirrored).
- Execution (LTF) zone: also an exciting leg-in, red for a buy (drop-base-rally), green for a sell (rally-base-drop).
- Test: every closed candle whose wick reaches the proximal line, also several in a row.
- Used up: a closed candle closes strictly inside the zone. The zone disappears one candle later, so that the next
  candle can still give entry type 2 or 3 (scanner.py removes it at once; it has no entry types 2 and 3).
- A close beyond the distal line does not end the zone. The zone is ignored while price is beyond it.
- Score: freshness 3 / 1.5 / 0 for 0 / 1 / 2 or more tests; strength 2 for a gap or a leg-out body of 70% or more,
  otherwise 1; base 2 / 1 / 0; execution zones +1 when the 20 EMA is inside. HTF zones with 2 or more tests stay (for
  the curve) with freshness 0; execution zones with 2 or more tests are not used.
- History (Settings, "History searched for zones"), as scanner.py reads it for each mode:

| Timeframe | From |
| --- | --- |
| 5 min, 15 min | one month back |
| 30 min, 1 hr, 2 hr, 4 hr | the first day of the current quarter |
| Day | ten years back, or the mode's start if later (Daily: five years back; Weekly: 2010) |
| Week, Month, Quarter, Half year, Year | 2000, or the mode's start if later (Weekly: 2010) |

  Zones whose base starts before that date are left out. Early in a quarter the intraday modes therefore see few
  zones, as scanner.py does. "All stored history" uses every zone instead.

GTF course PDF marking (choose "GTF course PDF" in Settings):

**Candles.** Base: body below 50% of the range. Exciting (explosive): body above 50%, green or red. Exactly 50% is neither.

**Gap candle (course page 25).** A candle with a small body that opens at least 0.25% of the price beyond the previous
candle's high (or low) and still closes beyond it counts as an exciting candle in the direction of the gap. The size is
the setting "Gap candle counts as exciting"; 0 switches the rule off. The course only says "significant gap", so 0.25%
is a starting value, not a course number.

**Zone.** An exciting leg-in, then 1 to 6 base candles, then an exciting leg-out. More than 6 base candles in a row give
no zone.

| Pattern | Leg-in | Leg-out | Kind |
| --- | --- | --- | --- |
| DBR, drop-base-rally | Down | Up | Demand, reversal |
| RBR, rally-base-rally | Up | Up | Demand, continuation |
| RBD, rally-base-drop | Up | Down | Supply, reversal |
| DBD, drop-base-drop | Down | Down | Supply, continuation |

**Closing rule (course page 24).** The leg-out must close beyond the whole leg-in candle: above its high for demand,
below its low for supply. Closing exactly on the high or low is not enough.

**Lines (course pages 14 to 20).** Body to wick. The proximal line is the highest base body for demand and the lowest
base body for supply. The distal line is the lowest base wick for demand, moved to the leg-out wick, or for a reversal
pattern the leg-in wick, when that wick is lower: the course's exceptional marking. Supply is the mirror with the
highest wick. For continuation patterns the leg-in wick is not used, because price came from beyond the zone.
The chart's zone details say which candle gave the distal line.

| Pattern | Distal line |
| --- | --- |
| DBR | Lowest low of leg-in, base and leg-out |
| RBR | Lowest low of base and leg-out |
| RBD | Highest high of leg-in, base and leg-out |
| DBD | Highest high of base and leg-out |

**Choosing.** The Backtest page reads both settings at once, so the same scan date can be checked under each; the
dashboard follows after "Save and rebuild all zones". The example the course marks as wrong on page 24 is a valid zone
under the scanner.py marking.

**Tests and freshness (PDF marking).** Checked on every closed candle after the zone forms:

| What happens | Result |
| --- | --- |
| A candle closes beyond the distal line | Violated |
| A candle reaches the proximal line | A test begins. Candles that keep reaching the zone belong to the same test |
| A candle no longer reaches the proximal line | The test is over |
| A second test is over | Used up ("tested twice") |

A candle that closes inside the zone no longer ends the zone: entry types 2 and 3 need exactly that candle.

**Trade score (PDF marking).** From the GTF course, out of 8:

| Part | Points |
| --- | --- |
| Freshness | Fresh 3, tested once 1.5, tested twice 0 |
| Strength | Price left with a gap 2, with two exciting candles 2, with one exciting candle and no gap 1 |
| Time at base | 1 to 3 base candles 2, 4 to 5 candles 1, more 0 |
| EMA | 1 when the 20 or the 50 EMA runs through the zone (execution zones only) |

- 7 or more takes entry type 1. 5 to 6.5 takes entry type 2 or 3. Below 5 there is no trade.
- The test that is going on is not counted against freshness: the zone is scored for the test it is in. Once that
  test is over, the zone is "tested once" and its score drops by 1.5.
- "Two exciting candles" means the candle straight after the leg-out is also exciting, in the same direction. It is
  known one candle after the zone forms, so a zone's score can rise from 6 to 7 on that candle.

**Modes.** Each mode names an HTF, an ITF and an LTF:

| Mode | HTF | ITF | LTF |
| --- | --- | --- | --- |
| 30 min | 30 min | 15 min | 5 min |
| Hourly | 1 hr | 30 min | 5 min |
| 2 hr | 2 hr | 30 min | 5 min |
| 4 hr | 4 hr | 1 hr | 15 min |
| Daily | Day | 1 hr | 30 min |
| Weekly | Week | Day | 1 hr |
| Monthly | Month | Week | Day |
| Quarterly | Quarter | Month | Day |
| Half yearly | Half year | Quarter | Week |
| Yearly | Year | Half year | Month |

15 minute candles are built from 5 minute ones and 2 hour candles from 30 minute ones.

**HTF and curve.** From the current price, the nearest HTF demand below it and the nearest HTF supply above it are
found: demand zones whose distal line is below the price (the highest proximal line wins), supply zones whose distal
line is above it (the lowest proximal line wins). Price inside a zone still counts. **Only zones that have never been
tested are used** (GTF PDF page 31, "nearest fresh supply and demand"): a zone price has come back to is skipped and
the next untested one further away is used. A candle that is still forming has not been counted as a test, so a zone
stays usable during the HTF candle that first reaches it. Settings, "Nearest HTF demand and supply for the curve",
can switch back to "any live zone", as scanner.py does. Tested zones stay on the chart either way.
The curve places price between the two proximal lines: very low, low, equilibrium, high, very high.
With only a demand zone left the state is bullish expansion, with only a supply zone bearish expansion, and the curve
is taken as equilibrium.

**Trends.** HTF: two or more of the last 20 closes above the supply is an uptrend, below the demand a downtrend.
ITF: slope of the 50 SMA over 7 bars (a shorter SMA, then the raw 7-bar slope, when there are fewer candles).

**Direction: location beats trend.** The curve decides the side at the edges, and the ITF only in the middle.

| Curve | Where the price is | ITF up | ITF down | ITF sideways |
| --- | --- | --- | --- | --- |
| Very low | Inside the HTF demand zone | Buy | Buy | Buy |
| Low | Lowest third above the demand proximal | Buy | Buy | Buy |
| Equilibrium | Middle third | Buy | Sell | No trade |
| High | Top third below the supply proximal | Sell | Sell | Sell |
| Very high | Inside the HTF supply zone | Sell | Sell | Sell |

- When only one HTF zone is left (an expansion) there is no curve: buy only with the ITF up and the HTF trend not
  down; sell only with the ITF down and the HTF trend not up.
- The earlier rule is still available under "Trade side rule" in Settings: the ITF decides (up buys, down sells,
  sideways is no trade) and the curve only blocks very high for buys and very low for sells. Saving the rule rescans
  every stock. With both rules you can run the same scan-date check twice and compare.
- The dashboard and the chart say what decided the side, and the chart labels the curve bands "low: buy",
  "equilibrium: ITF decides" and "high: sell".
- Under location beats trend a buy can fire while the ITF is falling, and a sell while it is rising.

**Execution zone.** On the LTF, a reversal pattern (drop-base-rally for a buy, rally-base-drop for a sell) with an
explosive leg-in, at most one earlier test, lying between price and the HTF zone (up to 10% of the HTF zone's width beyond
its distal line), and no more than 50% away from price.

**Entry types.** The stop loss is the distal line and the target is 2 times the distance from entry to stop loss
(setting "Target") for all three.

| Entry type | Used for a score of | Entry |
| --- | --- | --- |
| 1 | 7 or more | A limit at the proximal line, set and forget. Filled when price comes back to the zone |
| 2 | 5 to 6.5 | A candle closes inside the zone and the next candle opens inside it: entry at that open |
| 3 | 5 to 6.5 | A candle closes inside the zone and the next candle closes back beyond the proximal line: entry at that close |

- For a score of 5 to 6.5 you choose in Settings which of types 2 and 3 the plans, the alerts and paper trading use
  ("A score of 5 to 6.5 enters with"). Entry type 3 is the default. All three are always shown in the row details.
- "Inside the zone" means from the proximal line to the distal line, both included.
- Entries of types 2 and 3 are found by the scan when the candle closes, not by the live price. A type 2 entry is
  therefore known only after its candle has closed, although its price is that candle's open.
- An entry of type 2 or 3 belongs to one test. When the test ends without it, the plan waits for the next test, by
  which time the zone is "tested once" and is scored again.
- Until the entry happens, a type 2 or 3 plan holds the proximal line as its entry and a target measured from it.
  Both are replaced by the real entry and target when the entry happens. The chart labels such lines
  "waiting (proximal line)".

**Live states and alerts.** Approaching: price within 1% of the proximal line. Touching now: price at or inside the zone.
One alert for each, once per zone.

**Where this differs from the original scanner.py.**

- With the PDF marking a close beyond the distal line retires the zone; with the scanner.py marking it does not.
- Zones are stored and updated candle by candle, so tests and freshness are a history, not a recount on each scan.
- Price alerts (approaching, touching) are raised for untested zones only. A zone tested once can still be a setup
  when its score is 5 or more.
- The live candle is never used to form a zone or to count a test; live prices only drive the alerts.
- With the scanner.py marking, a zone that closed inside is removed one candle later than scanner.py removes it.
- Entries are entry types 1, 2 and 3 from the course, not scanner.py's aggressive, confirmation and conservative plans.

**Things worth knowing about these rules.**

- With no size test on exciting candles, many zones are found, most of them soon used up. The course marking finds
  fewer than the scanner.py one.
- Under the scanner.py marking a very thin zone can be touched many times without a close strictly inside it, so it
  stays (with freshness 0) and can still be the nearest HTF zone for the curve. "Smallest score for an HTF location
  zone" in Settings (0, as in scanner.py) can be raised to 5 to skip such zones.
- Where the distal line moves to a leg-in or leg-out wick the zone is wider: the stop loss is further away, the
  quantity for the same risk is smaller, and the target (2 times the risk) is further away too.
- Entry type 3 enters above the proximal line of a demand zone (below it for supply), so its risk is larger than the
  zone and its target is further away than entry type 1's. Entry type 2 enters inside the zone, so its risk is
  smaller than the zone and one wick to the distal line stops it.
- The course text for types 2 and 3 speaks of a first and a second candle. As built, any candle of the test that
  closes inside the zone can be the first one, so a type 3 entry can come several candles into a test.
- Not built: the stop loss with room beyond the distal line, the credibility filter and the trailing stop.
- Zones are tight, so costs take a visible share of each trade, and a gap through the stop can lose several times the
  planned risk.

## HTF contact

For every setup, two zones are checked against the same-side zones of higher timeframes: the setup's own (LTF) zone,
against every timeframe above it, and its mode's HTF zone, against every timeframe above that.

| Result | Rule | What is shown |
| --- | --- | --- |
| Inside | The zone lies completely within the higher zone | The timeframe |
| Overlap | The two price ranges share some prices | The shared range |
| Touch | No overlap, and the gap is at most 0.1% of price | The price where the edges meet |
| Near | The gap is at most 1% of price | The gap in percent, above or below |

- Only active zones count, one entry per higher timeframe (its strongest contact), nearest timeframe first.
- Scanner: an "HTF contact" column, the full list for both zones in the row details, and two filters: the execution
  zone's timeframe, and contact with any timeframe or with one chosen timeframe.
- Chart: selecting a zone draws its mode's HTF zone and every contacted higher zone, dashed and labelled, with one
  switch per timeframe.
- The two limits are under Zone rules in Settings. The field is information only: it does not change the score.
- Contacts are worked out when a timeframe is scanned. Press Scan all once after upgrading to fill them in.

## Speed

- A scan never blocks the pages: it runs in a worker thread with its own database session. So does the database
  part of a sync. Up to four instruments are downloaded side by side.
- The five-minute job skips every timeframe that received no new candle. Scan all and Rescan still replan everything.
- Zones found in a scan are written in one go at the end, not one by one.
- On the first scan of a stock, zones that were used up long ago are not stored at all. Violated and used-up zones are
  kept 10 days on 5 min, 15 on 15 min, 30 on 30 min, 45 on 1 hr, 60 on 2 hr, 90 on 4 hr and 120 on daily and above,
  then removed by the 16:00 job (and once in the background at start-up). Active zones are never removed.
  The backtest and replay rebuild zones from candles themselves, so they lose nothing.
- Measured on one instrument's full history on MySQL: first scan 60 s before, under 3 s now; a repeat scan 11 s
  before, under half a second now; zones stored about 10,600 before, about 500 now.

## Adding index stocks

- The members of an index are read from the official NSE Indices file (`niftyindices.com/IndexConstituent/...`) at the
  moment you press "Show its stocks", so the list is the current one. Nothing is hard-coded.
- Each row is matched to an Upstox instrument by ISIN, so a stock that changed its symbol is still found.
- If niftyindices.com refuses the request from your server, paste the symbols into the box below it instead.
- The Nifty 50 and Nifty Midcap 100 file addresses are confirmed. Nifty Bank and Nifty Next 50 follow the same naming
  and are untested.
- While a newly added stock is scanned for the first time, the pages can pause for a few seconds.

## Paper trading rules

- An order is created from a zone's current plan (Paper trade on the scanner), or automatically for every watched
  setup at or above a score you choose. One order or position per zone.
- Size: the quantity that loses your chosen share of capital (default 1% of 10,00,000) if the stop is hit.
- Fill, entry type 1: when the live price reaches the entry, at the entry price.
- Fill, entry types 2 and 3: when the scan records the entry, at the recorded price and candle time. The quantity is
  worked out again from the real entry, because the risk per share is then different. Price reaching the proximal
  line does not fill these orders.
- A pending order follows its plan if the plan changes, and is cancelled if the zone is violated or used up.
- Exit: stop at the live price seen when it is reached (a market exit), target at the target price (a limit exit),
  or Close now at the live price less the slippage setting.
- Safety net: after every 5 minute bar the stored candles are replayed against each order and position with the
  backtest's rules, so a fill or stop that happened between two price polls is not missed.
- Option positions bought from the Options page have no stop or target; close them yourself.

## Telegram

Create a bot with BotFather in Telegram and enter its token and your chat id on the Settings page (or set
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`). Send test alert confirms it works. Zone alerts and paper
fills are then sent to that chat. The token is stored in the database and never returned by any API.

## Options data

- Strikes, prices, open interest, IV and Greeks come live from the Upstox option chain, one underlying and one
  expiry per request. Expiries come from the instruments file.
- While the market is open, a snapshot of the nearest expiry is saved every 5 minutes for the indices in your list
  and for instruments with a watched setup. IV percentile is built from those snapshots and shows
  "building history" until 20 days are stored (it then grows to 252).

## Backtest: the scan-date check

The Backtest page has one form and one date. You pick stocks, a mode and a scan date; there is no start or end date.

- **Stocks:** every ready stock is ticked to begin with. The "All stocks" box ticks or clears them all, and shows a
  partial mark when only some are ticked.
- **As of the scan date:** the engine is run on the candles up to that date only, so the nearest HTF zones, the
  curve, the ITF trend, the LTF entry zone and its score are what the rules would have said that day.
- **After the scan date:** the entry type the trade score takes is followed through the later LTF candles with the
  usual fill rules.

| Score on the scan date | Followed | Shown as "not taken" |
| --- | --- | --- |
| 7 or more | Entry type 1: the first later candle that reaches the proximal line | Entry types 2 and 3 |
| 5 to 6.5 | Entry types 2 and 3, both, each from its own candle | Entry type 1 |
| Below 5 | Nothing: no trade on that date | All three |

- The entry has to come on the zone's next test (or the one going on at the scan date). After that test the zone is
  no longer what was scored, so the entry is "never entered".
- A type 2 entry is checked for its exit from the entry candle itself, because it enters at the open. A type 1 limit
  that is filled and stopped in the same candle counts as a loss.
- Each entry ends as target hit, stopped, still open at the last stored candle, never entered (with the reason), or not taken.
- The report shows totals per entry type and one row per stock, with a "Chart and replay" link that opens the chart on that scan date.
- **Past checks:** every check is saved and listed under the form, newest first, with its totals. Click one to open its
  report again exactly as it was; Delete removes it. They are kept in the backtest runs table.
- One date is one sample. It shows how the rules read a stock on a day you choose, not a win rate.

## Chart: scan date and replay

- Choosing a scan date above the chart shows the result as of that date, draws a vertical line there (switch:
  "Scan date line") and marks each entry type's entry and exit. "Back to today" returns to the live result.
- **Replay starts from the scan date and does not redraw the picture.** The nearest HTF demand and supply, the curve,
  the LTF zone, the entry, the stop loss and the target stay as they were on the scan date, and only the items ticked
  under "Show on the chart" are drawn. Unticking one during replay hides it at once.
- Candles after the scan date appear one at a time. When the replay reaches them, each entry is marked with an
  arrow, its target with a green marker and its stop loss with a red marker, and the event list says what happened
  and the result in R. At the end, entries that never happened are listed with the reason.
- Play, pause, step, speed and "Jump to next event" (the next entry or exit) work as before.
- With "All zones" ticked before starting, the older replay runs instead: the engine recalculates every zone of that
  one timeframe at each bar.

The date-range backtest (From and To) is no longer on the page. Its API and its saved runs are still in the database.

## How the backtest works

- The same engine and fill rules as live, on stored candles. No Upstox calls.
- Each chosen mode is run with its own HTF, ITF and LTF. A candle on a higher timeframe is used only after it has
  closed; a test checks that cutting the data off earlier leaves every earlier trade unchanged.
- Results are in R: 1 R is the distance from entry to stop. A cost (default 0.05% of price per side) is taken off every trade.
- Entry type 1: the limit exists only while the zone scores 7 or more and the mode allows the trade at the close of
  the previous candle; the candle that reaches the proximal line fills it. Entry types 2 and 3: the zone must score
  5 to 6.5 at the close of the previous candle, and the trade opens on the candle that completes the entry.
- A zone is traded once per run. `entry_plan` can be `rule` (the score decides, the default), `type1`, `type2` or `type3`.
- Entry and stop reached in the same candle counts as a full loss. Stop and target reached in the same later candle
  counts as the stop. A gap through the stop exits at the open.
- Trades still open at the end of the data are valued at the last close and kept out of the statistics.
- Breakdowns by zone type, mode, curve at entry and score show where the results come from.

## Tests

```
python -m pytest tests -q
```

144 tests. They run on a throwaway SQLite file and never touch your database, even with `DB_` values in `.env`.
To run them on MySQL, use an empty throwaway database (it is wiped at the start of the run):

```
TEST_DATABASE_URL='mysql+pymysql://user:pass@localhost:3306/scanner_test' python -m pytest tests -q
```

In this URL, special characters in the password must be encoded (`@` becomes `%40`).

The suite passes on SQLite. Earlier versions also passed on MySQL 8.0; the trade score and entry type update was
tested on SQLite only (it adds eleven plain columns and no new query type). It has not been run on MySQL 5.x; nothing in the code needs
version 8 (JSON is stored as text, and no window functions are used), but tell me if 5.x reports an error.
The sync, scan and backtest tests use a fake Upstox client with generated candles. That data exists only in the
tests; the application contains no made-up data.

## Not yet verified against live Upstox

The environment this was built in cannot reach Upstox, so these have not been run against the real API:

- The login exchange and profile check.
- Candle responses from the V3 historical and intraday endpoints (the URL format follows the docs).
- The instruments file download and its field names.
- The price quote call (`/v3/market-quote/ltp`) and the option chain call, whose response formats follow the docs.
- Sending to Telegram (the message format and the failure handling are tested; the real send is not).

Check during your first run: that 30-minute bars start at 09:15, and whether prices are adjusted for splits.

## Security

The app has no login of its own. Keep it bound to `127.0.0.1` as shown, or put it behind your own protection
before exposing it on a network: anyone who can open the pages can change settings and see your setups.
Keep `.env` out of version control.

## Layout

```
app/config.py            database selection, timeframes, GTF modes, default rules
app/db.py                engine, sessions, database creation, in-place schema upgrade
app/models.py            all 14 tables
app/upstox/client.py     login, candles, prices, option chain, instruments, throttle
app/data/                sync (incremental, backfill, repair, reset), resample, index member lists
app/engine/detect.py     candle types and zone detection
app/engine/lifecycle.py  tests, freshness, violated, used up, and the entries of types 2 and 3
app/engine/score.py      GTF score and zone type
app/engine/core.py       the engine, free of database code; scan, replay and backtest all drive it
app/engine/gtf.py        nearest HTF zones, curve, trends, direction, execution zones, entry plans
app/engine/contact.py    HTF contact: inside, overlap, touch, near
app/engine/confirm.py    confirmation score: checks from stored data and from the live option chain
app/engine/result.py     the full result of one stock in one mode, saved at each scan
app/status.py            where each stock stands: waiting, downloading, scanning, ready, failed
app/engine/plan.py       plans per zone and mode, from stored data
app/engine/scanner.py    scan service: drives the engine and saves the results
app/sim/                 fills (shared fill rules), backtest, scan-date check, replay, paper
app/options/chain.py     chain view, suggested strike, snapshots, IV percentile
app/alerts/telegram.py   Telegram sender and message text
app/live.py              price polling, alerts, paper fills, the page WebSocket hub
app/jobs/                scheduler (the cron jobs) and the job runner
app/web/                 seven pages; vendored Lightweight Charts 5.2.1 (Apache 2.0)
app/main.py              47 endpoints, two WebSockets and the pages
tests/                   81 tests
```