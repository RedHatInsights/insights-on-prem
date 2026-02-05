"""Database connection and session management."""
from functools import lru_cache
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

from app.config import get_settings

# Base class for declarative models
Base = declarative_base()


@lru_cache()
def get_engine():
    """
    Get or create the SQLAlchemy engine (singleton).

    Returns:
        SQLAlchemy Engine instance
    """
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        echo=settings.log_level == "DEBUG"
    )


@lru_cache()
def get_session_factory():
    """
    Get or create the session factory (singleton).

    Returns:
        SQLAlchemy sessionmaker instance
    """
    engine = get_engine()
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    """
    Dependency for FastAPI routes to get database session.

    Yields:
        Database session that will be closed after use.
    """
    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Initialize database by creating all tables."""
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
