from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# Arbitrary constant. Concurrent migrators queue on this lock instead of racing.
MIGRATION_LOCK_ID = 724001


def make_pool(database_url: str, min_size: int = 1, max_size: int = 10) -> ConnectionPool:
    return ConnectionPool(
        conninfo=database_url,
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": dict_row},
        open=True,
    )


def migrate(database_url: str, migrations_dir: str) -> list[int]:
    """Apply every migrations/NNN_*.sql file not yet recorded. Returns the versions applied."""
    applied_now: list[int] = []
    files = sorted(Path(migrations_dir).glob("*.sql"))
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version integer PRIMARY KEY,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
            for path in files:
                version = int(path.name.split("_", 1)[0])
                if version in done:
                    continue
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                applied_now.append(version)
    return applied_now
