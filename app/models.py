"""All 14 tables from the blueprint. Later phases use the paper, backtest and option tables.
Every text column has a length (MySQL needs one), prices are DOUBLE and volumes BIGINT."""
from datetime import datetime
import json

from sqlalchemy import BigInteger, Boolean, DateTime, Double, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.types import TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base


class JSONText(TypeDecorator):
    """JSON kept in a TEXT column, so it behaves the same on SQLite and on every MySQL version."""
    impl = Text().with_variant(LONGTEXT(), "mysql")      # MySQL TEXT stops at 64 KB; a saved report can be larger
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else json.dumps(value)

    def process_result_value(self, value, dialect):
        return None if value is None else json.loads(value)


class Instrument(Base):
    __tablename__ = "instruments"
    instrument_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    segment: Mapped[str | None] = mapped_column(String(20), index=True)
    instrument_type: Mapped[str | None] = mapped_column(String(20))
    lot_size: Mapped[int | None] = mapped_column(Integer)
    expiry: Mapped[datetime | None] = mapped_column(DateTime)
    strike: Mapped[float | None] = mapped_column(Double)
    underlying_key: Mapped[str | None] = mapped_column(String(64), index=True)


class Candle(Base):
    __tablename__ = "candles"
    instrument_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, primary_key=True)  # candle start, exchange time (IST)
    open: Mapped[float] = mapped_column(Double)
    high: Mapped[float] = mapped_column(Double)
    low: Mapped[float] = mapped_column(Double)
    close: Mapped[float] = mapped_column(Double)
    volume: Mapped[int] = mapped_column(BigInteger, default=0)
    oi: Mapped[int] = mapped_column(BigInteger, default=0)


class SyncState(Base):
    __tablename__ = "sync_state"
    instrument_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    last_candle_ts: Mapped[datetime | None] = mapped_column(DateTime)
    last_scanned_ts: Mapped[datetime | None] = mapped_column(DateTime)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime)
    gaps: Mapped[int] = mapped_column(Integer, default=0)


class Zone(Base):
    __tablename__ = "zones"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_key: Mapped[str] = mapped_column(String(64), index=True)
    timeframe: Mapped[str] = mapped_column(String(8), index=True)
    side: Mapped[str] = mapped_column(String(10))       # demand | supply
    pattern: Mapped[str] = mapped_column(String(8))    # DBR | RBR | RBD | DBD
    proximal: Mapped[float] = mapped_column(Double)
    distal: Mapped[float] = mapped_column(Double)
    base_start_ts: Mapped[datetime] = mapped_column(DateTime)
    base_count: Mapped[int] = mapped_column(Integer)
    created_ts: Mapped[datetime] = mapped_column(DateTime)  # leg-out candle start
    atr: Mapped[float] = mapped_column(Double)
    peak_distance: Mapped[float] = mapped_column(Double, default=0.0)
    touches: Mapped[int] = mapped_column(Integer, default=0)
    in_visit: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="fresh", index=True)  # fresh|tested|consumed|violated
    score: Mapped[float] = mapped_column(Double, default=0)   # GTF score, up to 8 with the EMA bonus
    swing_level: Mapped[float | None] = mapped_column(Double)
    broke_swing: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_zone_id: Mapped[int | None] = mapped_column(Integer)
    superseded_by: Mapped[int | None] = mapped_column(Integer)
    updated_ts: Mapped[datetime | None] = mapped_column(DateTime)
    # GTF fields (nullable so an existing table can be upgraded in place)
    label: Mapped[str | None] = mapped_column(String(12))            # TYPE_1 | TYPE_2_3 | WEAK
    has_gap: Mapped[bool | None] = mapped_column(Boolean)            # leg-out opened beyond the previous candle
    legout_body: Mapped[float | None] = mapped_column(Double)        # body share of the leg-out candle, 0 to 1
    legout_close: Mapped[float | None] = mapped_column(Double)
    legin_explosive: Mapped[bool | None] = mapped_column(Boolean)    # needed for an execution (LTF) zone
    distal_from: Mapped[str | None] = mapped_column(String(8))       # which candle gave the distal line: base, leg-in or leg-out
    wick_ext: Mapped[float | None] = mapped_column(Double)           # extreme of the first wick test, for the conservative plan
    wick_ts: Mapped[datetime | None] = mapped_column(DateTime)
    rej_entry: Mapped[float | None] = mapped_column(Double)          # close of the rejection candle
    rej_ts: Mapped[datetime | None] = mapped_column(DateTime)
    # Trade score and entry types from the GTF course (the four columns above are no longer written)
    legout_count: Mapped[int | None] = mapped_column(Integer)        # exciting candles that left the zone: 1 or 2
    inside_armed: Mapped[bool | None] = mapped_column(Boolean)       # the last candle closed inside the zone
    inside_ts: Mapped[datetime | None] = mapped_column(DateTime)     # that candle, and its close
    inside_close: Mapped[float | None] = mapped_column(Double)
    t2_entry: Mapped[float | None] = mapped_column(Double)           # entry type 2 of the current test: open of the next candle, inside the zone
    t2_ts: Mapped[datetime | None] = mapped_column(DateTime)
    t3_entry: Mapped[float | None] = mapped_column(Double)           # entry type 3 of the current test: close of the next candle, beyond the proximal line
    t3_ts: Mapped[datetime | None] = mapped_column(DateTime)


class ZoneEvent(Base):
    __tablename__ = "zone_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    zone_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    event_type: Mapped[str] = mapped_column(String(32))
    price: Mapped[float | None] = mapped_column(Double)
    old_value: Mapped[str | None] = mapped_column(String(255))
    new_value: Mapped[str | None] = mapped_column(String(255))


class TradePlan(Base):
    __tablename__ = "trade_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    zone_id: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    entry: Mapped[float] = mapped_column(Double)
    stop: Mapped[float] = mapped_column(Double)
    target: Mapped[float] = mapped_column(Double)
    target_zone_id: Mapped[int | None] = mapped_column(Integer)
    target_source: Mapped[str] = mapped_column(String(16), default="zone")  # zone | fixed_3r
    reward_risk: Mapped[float] = mapped_column(Double)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|triggered|target_hit|stopped|cancelled
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    location_pos: Mapped[float | None] = mapped_column(Double)
    trend: Mapped[str | None] = mapped_column(String(16))
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_ts: Mapped[datetime | None] = mapped_column(DateTime)
    grade: Mapped[str | None] = mapped_column(String(2))        # top-down grade A to D
    shared: Mapped[list | None] = mapped_column(JSONText)       # higher-timeframe zones of the same side that overlap
    mode: Mapped[str | None] = mapped_column(String(12), index=True)   # GTF analysis mode this plan belongs to
    plans: Mapped[dict | None] = mapped_column(JSONText)        # the three entry types as they stand for this zone
    entry_type: Mapped[str | None] = mapped_column(String(8))   # TYPE_1 | TYPE_2 | TYPE_3: the entry this plan waits for
    triggered_ts: Mapped[datetime | None] = mapped_column(DateTime)   # when that entry happened (candle time)
    context: Mapped[dict | None] = mapped_column(JSONText)      # curve, trends, market state and the HTF zone
    zone_state: Mapped[str | None] = mapped_column(String(16))  # JUST_FORMED | APPROACHING | TOUCHING_NOW | ACTIVE


class Rejection(Base):
    __tablename__ = "rejections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_key: Mapped[str] = mapped_column(String(64), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    ts: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str] = mapped_column(String(255))


class PaperOrder(Base):
    __tablename__ = "paper_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(Integer)
    instrument_key: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[int] = mapped_column(Integer)
    entry: Mapped[float] = mapped_column(Double)
    status: Mapped[str] = mapped_column(String(16), default="pending")   # pending | filled | cancelled
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    # added in phase 7 (nullable so an existing table can be upgraded in place)
    zone_id: Mapped[int | None] = mapped_column(Integer, index=True)
    timeframe: Mapped[str | None] = mapped_column(String(8))
    stop: Mapped[float | None] = mapped_column(Double)
    target: Mapped[float | None] = mapped_column(Double)
    checked_ts: Mapped[datetime | None] = mapped_column(DateTime)
    note: Mapped[str | None] = mapped_column(String(255))
    archived: Mapped[bool | None] = mapped_column(Boolean, default=False)
    entry_type: Mapped[str | None] = mapped_column(String(8))   # TYPE_1 is a limit at the proximal line; TYPE_2 and TYPE_3 fill when the candles confirm


class PaperTrade(Base):
    __tablename__ = "paper_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer)
    instrument_key: Mapped[str] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[int] = mapped_column(Integer)
    fill_price: Mapped[float] = mapped_column(Double)
    fill_ts: Mapped[datetime] = mapped_column(DateTime)
    stop: Mapped[float] = mapped_column(Double)
    target: Mapped[float] = mapped_column(Double)
    exit_price: Mapped[float | None] = mapped_column(Double)
    exit_ts: Mapped[datetime | None] = mapped_column(DateTime)
    result: Mapped[str | None] = mapped_column(String(16))   # win | loss, set when closed
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    # added in phase 7 and 8
    zone_id: Mapped[int | None] = mapped_column(Integer, index=True)
    timeframe: Mapped[str | None] = mapped_column(String(8))
    kind: Mapped[str | None] = mapped_column(String(8), default="zone")   # zone | option
    label: Mapped[str | None] = mapped_column(String(100))
    risk: Mapped[float | None] = mapped_column(Double)        # planned risk per unit, the 1 R of this trade
    pnl: Mapped[float | None] = mapped_column(Double)
    result_r: Mapped[float | None] = mapped_column(Double)
    exit_reason: Mapped[str | None] = mapped_column(String(16))   # stop | target | manual
    checked_ts: Mapped[datetime | None] = mapped_column(DateTime)
    history: Mapped[list | None] = mapped_column(JSONText)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    settings: Mapped[dict | None] = mapped_column(JSONText)
    summary: Mapped[dict | None] = mapped_column(JSONText)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|done|failed|cancelled
    progress: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    instrument_key: Mapped[str] = mapped_column(String(64))
    timeframe: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(10))
    score: Mapped[float] = mapped_column(Double)
    entry: Mapped[float] = mapped_column(Double)
    stop: Mapped[float] = mapped_column(Double)
    target: Mapped[float] = mapped_column(Double)
    entry_ts: Mapped[datetime] = mapped_column(DateTime)
    exit_ts: Mapped[datetime | None] = mapped_column(DateTime)
    exit_price: Mapped[float | None] = mapped_column(Double)
    result: Mapped[str | None] = mapped_column(String(16))   # win | loss | open
    result_r: Mapped[float | None] = mapped_column(Double)
    reward_risk: Mapped[float | None] = mapped_column(Double)
    pattern: Mapped[str | None] = mapped_column(String(8))
    zone_created_ts: Mapped[datetime | None] = mapped_column(DateTime)
    location_pos: Mapped[float | None] = mapped_column(Double)
    trend: Mapped[str | None] = mapped_column(String(16))
    mfe_r: Mapped[float | None] = mapped_column(Double)   # furthest move in favour before the exit, in R
    grade: Mapped[str | None] = mapped_column(String(2))
    loc_ok: Mapped[bool | None] = mapped_column(Boolean)
    trend_ok: Mapped[bool | None] = mapped_column(Boolean)
    shared_count: Mapped[int | None] = mapped_column(Integer)
    mode: Mapped[str | None] = mapped_column(String(12))
    curve: Mapped[str | None] = mapped_column(String(16))
    entry_type: Mapped[str | None] = mapped_column(String(16))
    label: Mapped[str | None] = mapped_column(String(12))
    zone_proximal: Mapped[float | None] = mapped_column(Double)   # the zone's own lines, for the trade chart
    zone_distal: Mapped[float | None] = mapped_column(Double)


class OptionSnapshot(Base):
    __tablename__ = "option_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    underlying_key: Mapped[str] = mapped_column(String(64), index=True)
    expiry: Mapped[str] = mapped_column(String(16))
    strike: Mapped[float] = mapped_column(Double)
    option_type: Mapped[str] = mapped_column(String(4))
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    oi: Mapped[float | None] = mapped_column(Double)
    iv: Mapped[float | None] = mapped_column(Double)
    ltp: Mapped[float | None] = mapped_column(Double)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSONText)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|done|failed
    progress: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)


class InstrumentStatus(Base):
    """Where each instrument of the stock list stands. It is shown on the pages only once it has been ready."""
    __tablename__ = "instrument_status"
    instrument_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="queued")   # queued | downloading | scanning | ready | failed
    ready: Mapped[bool] = mapped_column(Boolean, default=False)        # downloaded and scanned in full at least once
    detail: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class ModeResult(Base):
    """The full GTF result of one instrument in one analysis mode, saved at each scan so the dashboard opens at once."""
    __tablename__ = "mode_results"
    instrument_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    mode: Mapped[str] = mapped_column(String(12), primary_key=True)
    signal: Mapped[str | None] = mapped_column(String(12))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    data: Mapped[dict | None] = mapped_column(JSONText)


# How zones and plans are actually looked up: by instrument, timeframe and status; by zone and "current".
Index("ix_zones_key_tf_status", Zone.instrument_key, Zone.timeframe, Zone.status)
Index("ix_plans_zone_current", TradePlan.zone_id, TradePlan.is_current)