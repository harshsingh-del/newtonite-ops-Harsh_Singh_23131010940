from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.string import TextLoader
from psycopg_pool import ConnectionPool

from . import config

MIGRATIONS = Path(__file__).parent / "migrations"


def _configure(conn):
    # Return uuid columns as plain strings so ids compare and serialise like normal strings.
    conn.adapters.register_loader("uuid", TextLoader)


# `with pool.connection() as conn:` is one transaction: it commits when the block ends
# normally and rolls back if anything inside raises. Every request uses exactly one.
pool = ConnectionPool(
    config.DATABASE_URL,
    min_size=1,
    max_size=20,
    kwargs={"row_factory": dict_row},
    configure=_configure,
    open=True,
)


def migrate() -> None:
    """Apply each *.sql file once, in name order. Safe to run repeatedly and concurrently."""
    with pool.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(424242)")  # serialises concurrent migrators
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if conn.execute("SELECT 1 FROM schema_migrations WHERE name = %s", (path.name,)).fetchone():
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            print(f"applied {path.name}")

