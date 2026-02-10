"""Tests for upload endpoint."""
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


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


def test_upload_invalid_file_format():
    """Test upload with invalid file format."""
    files = {"file": ("test.txt", BytesIO(b"test data"), "text/plain")}

    response = client.post(
        "/api/ingress/v1/upload",
        files=files
    )

    assert response.status_code == 400
    # Custom exception handler puts the message in "error", not "detail"
    assert "tar" in response.json()["error"].lower()


def test_upload_no_filename():
    """Test upload without filename."""
    files = {"file": ("", BytesIO(b"test data"), "application/gzip")}

    response = client.post(
        "/api/ingress/v1/upload",
        files=files
    )

    # FastAPI returns 422 for empty filename (validation at framework level)
    assert response.status_code == 422
