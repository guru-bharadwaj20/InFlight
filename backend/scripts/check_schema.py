"""Fail loudly when the SQLAlchemy models drift from the migrated database.

prisma/schema.prisma owns the tables; backend/app/models.py mirrors them. That
duplication is only safe if something checks it, so run this after every
migration:

    docker compose exec backend python -m scripts.check_schema
"""

import asyncio
import sys

from sqlalchemy import inspect

from app.db import init_engine
from app.models import Base


async def main() -> int:
    engine = init_engine()
    problems: list[str] = []

    async with engine.connect() as conn:
        db_tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))

        for table in Base.metadata.sorted_tables:
            if table.name not in db_tables:
                problems.append(f"table {table.name!r} is missing from the database")
                continue

            db_columns = await conn.run_sync(
                lambda c, t=table.name: {col["name"] for col in inspect(c).get_columns(t)}
            )
            model_columns = {col.name for col in table.columns}

            for missing in sorted(model_columns - db_columns):
                problems.append(f"{table.name}.{missing} is in the model but not the database")
            for extra in sorted(db_columns - model_columns):
                problems.append(f"{table.name}.{extra} is in the database but not the model")

    await engine.dispose()

    if problems:
        print("Schema drift detected:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(f"OK — {len(Base.metadata.sorted_tables)} tables match the migrated schema.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
