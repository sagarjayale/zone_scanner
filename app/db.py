from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.schema import CreateColumn
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL

URL_OBJ = make_url(DATABASE_URL)
IS_SQLITE = URL_OBJ.get_backend_name() == "sqlite"

if IS_SQLITE:
    # Scans and stores run in worker threads, so SQLite must wait for a lock instead of failing, and use WAL
    # so that readers are not blocked by a writer.
    engine = create_engine(URL_OBJ, connect_args={"check_same_thread": False, "timeout": 60})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(conn, _record):
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
else:
    # pre_ping and recycle keep long-running jobs working after MySQL drops idle connections
    engine = create_engine(URL_OBJ, pool_pre_ping=True, pool_recycle=3600, pool_size=10, max_overflow=20)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def ensure_database() -> None:
    """On MySQL, create the database named in .env if it does not exist yet."""
    if URL_OBJ.get_backend_name() != "mysql":
        return
    server = create_engine(URL_OBJ._replace(database=None), isolation_level="AUTOCOMMIT")  # connect to the server, not a database
    name = URL_OBJ.database.replace("`", "")
    with server.connect() as conn:
        conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
    server.dispose()


def ensure_schema() -> None:
    """Create missing tables, and add columns that a newer version introduced to tables that
    already exist. Existing data is left alone; nothing is dropped or changed."""
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    quote = engine.dialect.identifier_preparer.quote
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    conn.execute(text(f"ALTER TABLE {quote(table.name)} ADD COLUMN {CreateColumn(col).compile(dialect=engine.dialect)}"))
    widen_columns()
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:        # indexes a newer version introduced
        have = {ix["name"] for ix in insp.get_indexes(table.name)}
        for index in table.indexes:
            if index.name not in have:
                index.create(engine)


def widen_columns() -> None:
    """Scores became decimal numbers (6.5 is a valid GTF score). On MySQL an existing INT column would round
    them, so it is changed to DOUBLE. SQLite stores decimals in any numeric column, so nothing is needed there."""
    if URL_OBJ.get_backend_name() != "mysql":
        return
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, column in (("zones", "score"), ("backtest_trades", "score")):
            kind = next((str(c["type"]).upper() for c in insp.get_columns(table) if c["name"] == column), "")
            if kind and "DOUBLE" not in kind:
                conn.execute(text(f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` DOUBLE"))
        for table, column in (("backtest_runs", "summary"), ("backtest_runs", "settings")):
            kind = next((str(c["type"]).upper() for c in insp.get_columns(table) if c["name"] == column), "")
            if kind and "LONGTEXT" not in kind:      # a saved scan check for many stocks does not fit in 64 KB
                conn.execute(text(f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` LONGTEXT"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()