"""Tests for upload endpoint."""

import tempfile
from io import BytesIO
from unittest.mock import Mock

import pytest
from app.config import AppConfig
from app.main import app
from app.services.upload_service import UploadService
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

client = TestClient(app)


@pytest.fixture
def upload_service():
    """Set up a real UploadService for integration tests."""
    config = AppConfig(temp_upload_dir=tempfile.gettempdir())
    app.state.upload_service = UploadService(
        processor_service=Mock(),
        config=config,
        session_factory=Mock(spec=sessionmaker),
    )


def test_health_endpoint():
    """Test health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_root_endpoint():
    """Test root endpoint."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "insights-on-premise"
    assert data["status"] == "running"


def test_upload_invalid_file_format(upload_service):
    """Test upload with invalid file format."""
    files = {"file": ("test.txt", BytesIO(b"test data"), "text/plain")}

    response = client.post("/api/ingress/v1/upload", files=files)

    assert response.status_code == 400
    assert "tar" in response.json()["error"].lower()


def test_upload_no_filename(upload_service):
    """Test upload without filename."""
    files = {"file": ("", BytesIO(b"test data"), "application/gzip")}

    response = client.post("/api/ingress/v1/upload", files=files)

    # FastAPI returns 422 for empty filename (validation at framework level)
    assert response.status_code == 422
