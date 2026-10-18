"""Live-stack probe: count ledgered emulation runs (parameter-bound).

Doubles as the scanner-regression sentinel for the inline single-line
``%s``-bound SQL style this service uses everywhere (same discipline as
scripts/migrate.py:165).
"""

from psycopg import AsyncConnection


async def emulation_run_count(conn: AsyncConnection) -> int:
    """Number of rows in emulator.emulation_runs (0 when the table is absent)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM emulator.emulation_runs")
        row = await cur.fetchone()
        return int(row[0]) if row else 0
