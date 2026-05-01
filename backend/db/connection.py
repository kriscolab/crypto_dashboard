from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
import logging

from config.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.db_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,         # reconnect on stale connections
    echo=settings.environment == "development",
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncSession:
    """FastAPI dependency - yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_db_connection() -> bool:
    """Health check - verify TimescaleDB is reachable."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("SELECT 1"))
            result.fetchone()
            # Verify TimescaleDB extension
            result = await session.execute(
                text("SELECT extname FROM pg_extension WHERE extname = 'timescaledb'")
            )
            row = result.fetchone()
            if not row:
                logger.warning("TimescaleDB extension not found")
                return False
        return True
    except Exception as e:
        logger.error(f"DB health check failed: {e}")
        return False


async def execute_raw(sql: str, params: dict = None):
    """Execute raw SQL - useful for TimescaleDB-specific queries."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        await session.commit()
        return result
