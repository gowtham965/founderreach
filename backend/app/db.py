from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings

_settings = get_settings()
_connect_args = {"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {}

engine = create_engine(_settings.database_url, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Tiny forward-only migration: add new nullable columns to existing tables.
    (Use Alembic once the schema settles.)"""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in existing and col.nullable:
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col.type.compile(engine.dialect)}'))
                    if (table.name, col.name) in _BACKFILL:
                        conn.execute(text(f'UPDATE {table.name} SET "{col.name}" = {_BACKFILL[(table.name, col.name)]}'))
            # ALTER TABLE can't add a UNIQUE column, so added columns get a unique index instead.
            # Checked every start: also repairs databases where the column was added before this existed.
            for col in table.columns:
                if col.unique and col.nullable and not col.primary_key:
                    conn.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS uq_{table.name}_{col.name} ON {table.name} ("{col.name}")'))


# Values for rows that existed before a column was added.
_BACKFILL = {
    # accounts from before email confirmation existed count as confirmed, so nobody gets locked out
    ("users", "email_verified_at"): "CURRENT_TIMESTAMP",
}
