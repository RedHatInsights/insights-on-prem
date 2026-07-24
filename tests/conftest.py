"""Pytest configuration and fixtures."""

import pytest
from app.database import Base, get_db
from app.main import app
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture(scope="session")
def test_engine():
    """Create a shared test engine for the entire test session."""
    engine = create_engine(
        "sqlite:///",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="session")
def test_session_factory(test_engine):
    """Create a shared session factory for the entire test session."""
    return sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(scope="function")
def database(test_engine, test_session_factory):
    """
    Provide a transactional database session for each test.

    Each test runs inside a transaction that is rolled back after the test,
    ensuring test isolation without recreating the schema.

    :return: SQLAlchemy Session instance for testing
    """
    connection = test_engine.connect()
    transaction = connection.begin()
    session = test_session_factory(bind=connection)

    def override_get_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    yield session

    session.close()
    transaction.rollback()
    connection.close()
    app.dependency_overrides.clear()
