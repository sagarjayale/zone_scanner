"""Trailing stop loss, shared by paper trading and the scan-date check on the Backtest page, so both move the stop
the same way.

Off by default: with trail_method "off" nothing here runs and every trade keeps its first stop and its target.

The rules (a long; a short is the mirror image):
- R is the distance from the entry to the first stop.
- Trailing starts once price has reached entry + trail_start_r R (1 R by default). Until then the first stop stands.
- The stop is worked out again only when a candle of the trailing timeframe closes, from closed candles alone, so a
  check of a past date never sees a price before it happened. Candles count from the first one that opened after the fill.
- The stop only ever moves in the trade's favour, and never below break-even once trailing has started.
- Break-even includes the charges: entry x (1 + round-trip charges as a share of the trade value), so a break-even
  exit is about 0 R after charges.
- Within a candle the stop is checked before it is moved (the caller does that), the cautious assumption.
- trail_keep_target false removes the fixed target once trailing has started, so the trade can run.

Methods:
- step:  stop = entry + (best R reached - 1), rounded down to 0.5 R (at +1 R break-even, +1.5 R -> +0.5 R, +2 R -> +1 R).
- swing: stop just under each new swing low: a candle whose low is below the trail_swing candles on each side,
         confirmed trail_swing candles later, less 0.1 of the average candle range.
- atr:   stop = highest price since the entry - trail_atr_mult x ATR(14) of the trailing timeframe.
- lock:  steps of trail_lock_pct % of the distance from the entry to the first target. Once the best price has passed
         a step by trail_lock_gap_pct % of one step, the stop moves to that step. Entry 100, target 200, 10 %, gap 20 %:
         one step is 10, the gap 2; at 112 the stop goes to 110, at 122 to 120, at 152 to 150. trail_start_r is not used.
         trail_lock_target "move": the trade does not close at the target; each time the best price reaches it, the
         target moves forward one step (200 -> 210 -> 220) and only the locked stop ends the trade.
         "close": the trade closes at the target as usual.

         trail_lock_behind: the stop stays one step behind instead (step 1 locks break-even, step 2 locks step 1, ...).

Timeframes: "live" (the default) moves the stop on each live price at once, with no wait for a candle; the stored
5-minute candles are the backup for anything the live prices missed (a past-date check has no live prices and uses the
LTF candles). "ltf" moves the stop on the trade's own (LTF) candles, "itf" on the mode's middle timeframe."""
import math

METHODS = ("off", "step", "swing", "atr", "lock")
TIMEFRAMES = ("live", "ltf", "itf")
LOCK_TARGET = ("move", "close")
OPTION_KEYS = ("trail_method", "trail_start_r", "trail_keep_target", "trail_atr_mult", "trail_swing", "trail_tf",
               "trail_lock_pct", "trail_lock_gap_pct", "trail_lock_target", "trail_lock_behind")


def options(source: dict) -> dict:
    """The trailing options from settings (or a saved check), with their defaults."""
    get = source.get
    return {"trail_method": get("trail_method") if get("trail_method") in METHODS else "off",   # missing: off (old saved runs)
            "trail_start_r": float(get("trail_start_r") if get("trail_start_r") is not None else 1.0),
            "trail_keep_target": bool(get("trail_keep_target")) if get("trail_keep_target") is not None else True,
            "trail_atr_mult": float(get("trail_atr_mult") or 2.0),
            "trail_swing": int(get("trail_swing") or 2),
            "trail_tf": get("trail_tf") if get("trail_tf") in TIMEFRAMES else "ltf",   # the settings default is "live"
            "trail_lock_pct": float(get("trail_lock_pct") or 10.0),
            "trail_lock_gap_pct": float(get("trail_lock_gap_pct") if get("trail_lock_gap_pct") is not None else 20.0),
            "trail_lock_target": get("trail_lock_target") if get("trail_lock_target") in LOCK_TARGET else "move",
            "trail_lock_behind": bool(get("trail_lock_behind"))}


def is_on(opts: dict | None) -> bool:
    return bool(opts) and (opts.get("trail_method") or "off") != "off"


def no_target_from_fill(opts: dict) -> bool:
    """Lock with "move": the fixed target never closes the trade."""
    return is_on(opts) and opts.get("trail_method") == "lock" and opts.get("trail_lock_target", "move") == "move"


class Trailer:
    """The trailing stop of one open trade. Feed it the closed candles of the trailing timeframe, in order, starting
    with the first candle that opened after the fill; it returns the new stop when the stop moves.
    prior: (high, low, close) of candles before the fill, for the ATR. cost_frac: round-trip charges as a share of
    the trade value, for break-even. target: the first target (the lock method measures its steps from it)."""

    def __init__(self, side: str, entry: float, stop: float, opts: dict, prior: list | tuple = (),
                 target: float | None = None, cost_frac: float = 0.0):
        self.long = side == "demand"
        self.entry, self.first_stop, self.stop = float(entry), float(stop), float(stop)
        self.risk = abs(self.entry - self.first_stop)
        self.method = opts.get("trail_method") or "off"
        self.start_r = float(opts.get("trail_start_r") or 1.0)
        self.keep_target = bool(opts.get("trail_keep_target", True))
        self.atr_mult = float(opts.get("trail_atr_mult") or 2.0)
        self.swing = max(1, int(opts.get("trail_swing") or 2))
        cost_frac = max(0.0, float(cost_frac or 0.0))
        self.break_even = self.entry * (1 + cost_frac) if self.long else self.entry * (1 - cost_frac)
        self.bars = [tuple(map(float, b[:3])) for b in list(prior)[-20:]]   # (high, low, close), for the ATR
        self.after: list[tuple] = []                                       # candles since the fill, for the swings
        self.best = self.entry
        self.active = False
        self.moves = 0
        self.why = ""
        self.target = float(target) if target is not None else None
        self.first_target = self.target
        dist = abs(self.target - self.entry) if self.target is not None else 0.0
        self.lock_step = dist * float(opts.get("trail_lock_pct") or 10.0) / 100.0
        gap_pct = opts.get("trail_lock_gap_pct")
        self.lock_gap = self.lock_step * float(gap_pct if gap_pct is not None else 20.0) / 100.0
        self.lock_move = (opts.get("trail_lock_target") or "move") == "move"
        self.lock_behind = bool(opts.get("trail_lock_behind"))
        self.locked = 0
        self.target_moves = 0

    @property
    def best_r(self) -> float:
        if self.risk <= 0:
            return 0.0
        return (self.best - self.entry) / self.risk if self.long else (self.entry - self.best) / self.risk

    @property
    def target_removed(self) -> bool:
        """True when the fixed target no longer closes the trade. Lock with "move": from the fill on."""
        if self.method == "lock":
            return self.lock_move and self.lock_step > 0
        return self.active and not self.keep_target

    def live_target(self) -> float:
        """The target that closes the trade now: out of reach (infinity) when it was removed."""
        if self.target_removed:
            return math.inf if self.long else -math.inf
        return self.target if self.target is not None else (math.inf if self.long else -math.inf)

    def _lock(self) -> tuple[float | None, str]:
        if self.lock_step <= 0:
            return None, ""
        sign = 1 if self.long else -1
        gone = (self.best - self.entry) * sign
        n = int(math.floor((gone - self.lock_gap) / self.lock_step + 1e-9))
        if n < 1:
            return None, ""
        self.locked = max(self.locked, n)
        held = self.locked - 1 if self.lock_behind else self.locked      # one step behind: step 1 holds break-even
        level = self.entry + sign * held * self.lock_step
        pct = held * self.lock_step / abs(self.first_target - self.entry) * 100
        return level, f"lock: step {self.locked}, {pct:g}% of the target locked, stop at +{held * self.lock_step / self.risk:.2f} R"

    def _move_target(self) -> None:
        if not (self.lock_move and self.lock_step > 0 and self.target is not None):
            return
        sign = 1 if self.long else -1
        while (self.best - self.target) * sign >= -1e-12 * max(1.0, abs(self.target)):
            self.target += sign * self.lock_step
            self.target_moves += 1

    def _better(self, a: float, b: float) -> float:
        return max(a, b) if self.long else min(a, b)

    def _atr(self, n: int = 14) -> float:
        bars = self.bars[-(n + 1):]
        if len(bars) < 2:
            return abs(bars[-1][0] - bars[-1][1]) if bars else 0.0
        ranges = [max(h - l, abs(h - bars[k - 1][2]), abs(l - bars[k - 1][2])) for k, (h, l, _c) in enumerate(bars) if k > 0]
        return sum(ranges) / len(ranges)

    def _avg_range(self, n: int = 14) -> float:
        bars = self.bars[-n:]
        return sum(h - l for h, l, _c in bars) / len(bars) if bars else 0.0

    def _candidate(self) -> tuple[float | None, str]:
        sign = 1 if self.long else -1
        if self.method == "step":
            locked = max(0.0, math.floor((self.best_r - 1.0) / 0.5 + 1e-9) * 0.5)
            return self.entry + sign * locked * self.risk, f"step: best +{self.best_r:.2f} R, stop at +{locked:g} R"
        if self.method == "atr":
            atr = self._atr()
            return self.best - sign * self.atr_mult * atr, f"ATR: best {self.best:.2f} - {self.atr_mult:g} x ATR {atr:.2f}"
        if self.method == "swing":
            n, bars = self.swing, self.after
            k = len(bars) - 1 - n
            if k >= n:
                lows = [b[1] for b in bars] if self.long else [b[0] for b in bars]
                here, around = lows[k], lows[k - n:k] + lows[k + 1:k + n + 1]
                if all(here < x for x in around) if self.long else all(here > x for x in around):
                    pad = 0.1 * self._avg_range()
                    return here - sign * pad, f"swing {'low' if self.long else 'high'} {here:.2f}"
        return None, ""

    def close_bar(self, high: float, low: float, close: float) -> float | None:
        """One closed candle of the trailing timeframe. Returns the new stop if it moved, else None."""
        bar = (float(high), float(low), float(close))
        self.bars = (self.bars + [bar])[-40:]
        self.after.append(bar)
        self.best = max(self.best, bar[0]) if self.long else min(self.best, bar[1])
        return self._update()

    def see_price(self, price: float) -> float | None:
        """A live price (no candle): the best price and the stop move at once. Lock and step only need the best
        price; swing and ATR wait for their candles. Returns the new stop if it moved, else None."""
        price = float(price)
        self.best = max(self.best, price) if self.long else min(self.best, price)
        if self.method not in ("lock", "step"):
            return None
        return self._update()

    def _update(self) -> float | None:
        if self.method == "off" or self.risk <= 0:
            return None
        if self.method == "lock":
            self._move_target()
            candidate, why = self._lock()
            if candidate is None:
                return None
            self.active = True
        else:
            if not self.active:
                if self.best_r + 1e-9 < self.start_r:
                    return None
                self.active = True
            candidate, why = self._candidate()
        floor = self.break_even
        wanted = floor if candidate is None else self._better(candidate, floor)
        new = self._better(self.stop, wanted)
        if abs(new - self.stop) <= 1e-9 * max(1.0, abs(self.stop)):
            return None
        self.stop, self.moves = new, self.moves + 1
        self.why = why if candidate is not None and self._better(candidate, floor) == candidate else "break-even (entry plus charges)"
        return new

    def state(self) -> dict:
        return {"stop": self.stop, "moves": self.moves, "best": self.best, "best_r": round(self.best_r, 3),
                "active": self.active, "target": self.target, "target_removed": self.target_removed, "why": self.why}


def ladder(side: str, entry: float, first_stop: float, first_target: float, opts: dict, stop: float,
           target: float | None = None, best: float | None = None) -> dict:
    """The lock method as a step ladder, for the Paper page. Pure numbers from the settings the trade kept at its fill,
    the stop as it stands now and the best price seen at a candle close.

    step = Lock % of entry -> first target; gap = Gap % of one step. Step k locks the stop at entry + k x step once the
    best price has passed entry + k x step + gap. steps_to_target: the steps from the entry to the first target (a
    25 % lock has 4). steps: the steps locked now, read from the stop itself (so a stop moved by hand is shown as it
    is). Beyond the first target ("move"), the steps keep counting: step 5 of a 4-step ladder is 1 beyond the target."""
    sign = 1 if side == "demand" else -1
    entry, first_stop, first_target = float(entry), float(first_stop), float(first_target)
    dist = abs(first_target - entry)
    step = dist * float(opts.get("trail_lock_pct") or 10.0) / 100.0
    gap_pct = opts.get("trail_lock_gap_pct")
    gap = step * float(gap_pct if gap_pct is not None else 20.0) / 100.0
    if step <= 0:
        return {"method": "lock", "step": 0.0, "steps": 0, "steps_to_target": 0, "levels": []}
    to_target = int(math.floor(dist / step + 1e-9))
    gone = (float(stop) - entry) * sign
    behind = bool(opts.get("trail_lock_behind"))
    steps = max(0, int(math.floor(gone / step + 1e-9)))
    if behind and gone >= -1e-9:          # one step behind: a stop at break-even is step 1, at step k it is step k + 1
        steps += 1
    best = float(best) if best is not None else entry
    reached = (best - first_target) * sign >= -1e-9
    move = (opts.get("trail_lock_target") or "move") == "move"
    top = max(to_target, steps + 1)
    return {"method": "lock", "step": step, "gap": gap, "steps": steps, "steps_to_target": to_target,
            "beyond_target": max(0, steps - to_target), "levels": [round(entry + sign * k * step, 4) for k in range(1, top + 1)],
            "next_stop": round(entry + sign * (steps if behind else steps + 1) * step, 4), "behind": behind, "next_trigger": round(entry + sign * ((steps + 1) * step + gap), 4),
            "target_reached": reached, "first_target": first_target, "first_stop": first_stop,
            "target": float(target) if target is not None else first_target, "target_mode": "move" if move else "close"}
