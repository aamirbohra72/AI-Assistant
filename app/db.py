import uuid
from collections.abc import AsyncIterator

from sqlalchemy import MetaData
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings
from app.tls import client_ssl_context

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# libpq-only options that asyncpg rejects; TLS is configured via connect_args instead.
_LIBPQ_ONLY_PARAMS = {"sslmode", "channel_binding"}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def build_async_url(raw: str) -> URL:
    url = make_url(raw)
    query = {k: v for k, v in url.query.items() if k not in _LIBPQ_ONLY_PARAMS}
    query["prepared_statement_cache_size"] = "0"
    return url.set(drivername="postgresql+asyncpg", query=query)


def make_engine() -> AsyncEngine:
    return create_async_engine(
        build_async_url(get_settings().DATABASE_URL),
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={
            "ssl": client_ssl_context(),
            # Neon's pooler is PgBouncer (transaction mode): avoid reusing named prepared statements.
            "statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
        },
    )


engine = make_engine()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
