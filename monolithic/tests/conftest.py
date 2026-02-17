"""Pytest configuration and fixtures."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db, get_engine, get_session_factory
from app.main import app


@pytest.fixture(scope="function")
def database():
    """
    Create a test database for each test.

    :return: SQLAlchemy Session instance for testing
    """
    SQLALCHEMY_DATABASE_URL = "sqlite:///"

    test_engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        connect_args={"check_same_thread": False}
    )

    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    # Create tables
    Base.metadata.create_all(bind=test_engine)

    # Override the get_db dependency
    def override_get_db():
        try:
            db = TestingSessionLocal()
            yield db
        finally:
            db.close()

    # Override database factory functions for testing
    def override_get_engine():
        return test_engine

    def override_get_session_factory():
        return TestingSessionLocal

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_engine] = override_get_engine
    app.dependency_overrides[get_session_factory] = override_get_session_factory

    yield TestingSessionLocal()

    # Cleanup
    Base.metadata.drop_all(bind=test_engine)
    app.dependency_overrides.clear()
