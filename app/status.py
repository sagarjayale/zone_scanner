"""Readiness of each instrument in the stock list. An instrument appears on the dashboard, chart, paper and
backtest pages only after it has been downloaded and scanned in full once. Later updates never hide it again."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .data.sync import now_ist
from .models import InstrumentStatus, SyncState


def mark(db: Session, key: str, state: str, detail: str | None = None) -> None:
    row = db.get(InstrumentStatus, key)
    if row is None:
        row = InstrumentStatus(instrument_key=key, ready=False)
        db.add(row)
    row.state, row.detail, row.updated_at = state, (detail or "")[:255] or None, now_ist()
    if state == "ready":
        row.ready = True
    db.commit()


def ready_keys(db: Session, universe: list[str]) -> list[str]:
    ok = set(db.scalars(select(InstrumentStatus.instrument_key).where(InstrumentStatus.ready.is_(True))))
    return [k for k in universe if k in ok]


def overview(db: Session, universe: list[str]) -> dict[str, dict]:
    rows = {r.instrument_key: r for r in db.scalars(select(InstrumentStatus).where(InstrumentStatus.instrument_key.in_(universe)))}
    return {k: {"state": rows[k].state if k in rows else "queued", "ready": bool(rows[k].ready) if k in rows else False,
                "detail": rows[k].detail if k in rows else None, "updated_at": rows[k].updated_at if k in rows else None} for k in universe}


def adopt_existing(db: Session, universe: list[str]) -> None:
    """Instruments scanned by an earlier version have no status row yet: those with scanned daily candles are ready."""
    have = set(db.scalars(select(InstrumentStatus.instrument_key)))
    for key in universe:
        if key in have:
            continue
        st = db.get(SyncState, (key, "1d"))
        done = st is not None and st.last_scanned_ts is not None
        db.add(InstrumentStatus(instrument_key=key, state="ready" if done else "queued", ready=done, updated_at=now_ist()))
    for row in db.scalars(select(InstrumentStatus).where(InstrumentStatus.state.in_(("downloading", "scanning")))):
        row.state, row.detail = ("ready", None) if row.ready else ("queued", "interrupted by a restart")   # nothing is running at start-up
    db.commit()
