"""Apply migrations/NNN_*.sql in order, once each. Runs as gateway_owner (one-shot compose service)."""
import asyncio, os, pathlib, asyncpg

async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        await conn.execute("SELECT pg_advisory_lock(424242)")  # one migrator at a time
        await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                           "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
        for f in sorted(pathlib.Path(__file__).with_name("migrations").glob("*.sql")):
            if f.stem in done:
                continue
            async with conn.transaction():
                await conn.execute(f.read_text())
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", f.stem)
            print("applied", f.stem)
        print("migrations up to date")
    finally:
        await conn.close()

asyncio.run(main())
