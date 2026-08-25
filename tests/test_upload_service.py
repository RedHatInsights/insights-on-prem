"""Tests for UploadService."""

import os
import tempfile
from queue import Full
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import AppConfig
from app.exceptions import ProcessorBusyError, ValidationError
from app.schemas import UploadResponse
from app.services.upload_service import UploadService


@pytest.fixture
def mock_archive_queue():
    """Create a mock archive processing queue."""
    return Mock()


@pytest.fixture
def test_config():
    """Create test configuration."""
    return AppConfig(
        max_file_size=100 * 1024 * 1024,  # 100MB
        temp_upload_dir=tempfile.gettempdir(),
    )


@pytest.fixture
def upload_service(test_config, mock_archive_queue):
    """Create UploadService instance with mocks."""
    return UploadService(
        config=test_config,
        archive_queue=mock_archive_queue,
    )


@pytest.mark.asyncio
async def test_process_upload_success(upload_service, mock_archive_queue):
    """Test successful upload scheduling."""
    test_data = b"test archive"
    mock_file = Mock()
    mock_file.filename = "test.tar.gz"
    mock_file.read = AsyncMock(side_effect=[test_data, b""])

    result = await upload_service.process_upload(mock_file, "req-123")

    assert isinstance(result, UploadResponse)
    assert result.request_id == "req-123"
    assert result.status == "accepted"

    mock_archive_queue.put_nowait.assert_called_once()
    temp_path, request_id = mock_archive_queue.put_nowait.call_args[0][0]
    assert request_id == "req-123"
    assert temp_path.endswith(".tar.gz")
    os.remove(temp_path)


@pytest.mark.asyncio
async def test_process_upload_enqueues_archive(upload_service, mock_archive_queue):
    """Test that processing is enqueued on the archive queue."""
    test_data = b"test archive"
    mock_file = Mock()
    mock_file.filename = "test.tar.gz"
    mock_file.read = AsyncMock(side_effect=[test_data, b""])

    await upload_service.process_upload(mock_file, "req-123")

    mock_archive_queue.put_nowait.assert_called_once()
    temp_path, request_id = mock_archive_queue.put_nowait.call_args[0][0]
    assert request_id == "req-123"
    os.remove(temp_path)


@pytest.mark.asyncio
async def test_process_upload_validation_error(upload_service, mock_archive_queue):
    """Test that validation errors are raised."""
    mock_file = Mock()
    mock_file.filename = "test.zip"  # Invalid format

    with pytest.raises(ValidationError):
        await upload_service.process_upload(mock_file, "req-123")

    mock_archive_queue.put_nowait.assert_not_called()


@pytest.mark.asyncio
async def test_process_upload_queue_full_raises_busy(tmp_path):
    """Test that a full queue raises ProcessorBusyError and removes the temp file."""
    mock_queue = Mock()
    mock_queue.put_nowait.side_effect = Full
    config = AppConfig(temp_upload_dir=str(tmp_path))
    service = UploadService(config=config, archive_queue=mock_queue)

    mock_file = Mock()
    mock_file.filename = "test.tar.gz"
    mock_file.read = AsyncMock(side_effect=[b"test archive", b""])

    with pytest.raises(ProcessorBusyError, match="retry later"):
        await service.process_upload(mock_file, "req-123")

    assert list(tmp_path.iterdir()) == []
