"""Tests for upload endpoint."""

import tempfile
from io import BytesIO
from queue import Full
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.main import app
from app.services.upload_service import UploadService

client = TestClient(app)


@pytest.fixture
def upload_service():
    """Set up a real UploadService for integration tests."""
    config = AppConfig(temp_upload_dir=tempfile.gettempdir())
    app.state.upload_service = UploadService(
        config=config,
        archive_queue=Mock(),
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


def test_upload_processor_busy(upload_service):
    """Test upload returns 503 with Retry-After when the processor queue is full."""
    mock_queue = Mock()
    mock_queue.put_nowait.side_effect = Full
    app.state.upload_service = UploadService(
        config=AppConfig(temp_upload_dir=tempfile.gettempdir()),
        archive_queue=mock_queue,
    )

    files = {"file": ("test.tar.gz", BytesIO(b"test data"), "application/gzip")}
    response = client.post("/api/ingress/v1/upload", files=files)

    assert response.status_code == 503
    assert "busy" in response.json()["error"].lower()
    assert response.headers["retry-after"] == "60"
