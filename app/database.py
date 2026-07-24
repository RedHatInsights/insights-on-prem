"""Database connection and session management."""

from collections.abc import Generator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, sessionmaker

# Base class for declarative models
Base = declarative_base()


def init_db(database_url: str):
    """Create engine, session factory, and all tables.

    :param database_url: PostgreSQL connection URL
    :return: Tuple of (engine, session_factory)
    """
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return engine, session_factory


def get_db(request: Request) -> Generator[Session, None, None]:
    """Dependency for FastAPI routes to get database session.

    :return: Database session that will be closed after use
    """
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
