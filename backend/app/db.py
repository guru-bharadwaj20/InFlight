from collections.abc import AsyncIterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .config import get_settings


def to_async_dsn(url: str) -> str:
    """Turn a Prisma-style connection string into one asyncpg accepts.

    Two differences to reconcile: the driver has to be named explicitly in the
    scheme, and Prisma's `?schema=` query parameter is not a libpq option, so
    asyncpg rejects it.
    """
    parts = urlsplit(url)

    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"

    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in {"schema", "sslmode", "connection_limit"}]

    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            to_async_dsn(settings.database_url),
            pool_pre_ping=True,
            # Every concurrent job holds a session while it assembles context and
            # commits, so the pool needs room for the per-conversation job cap.
            pool_size=10,
            max_overflow=20,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_factory()() as session:
        yield session
