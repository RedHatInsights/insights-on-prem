"""Tests for UploadService."""
import os
import tempfile
from unittest.mock import Mock, AsyncMock

import pytest
from fastapi import BackgroundTasks

from app.config import AppConfig
from app.exceptions import ValidationError
from app.schemas import UploadResponse
from app.services.upload_service import UploadService


@pytest.fixture
def mock_processor_service():
    """Create a mock ProcessorService."""
    service = Mock()
    service.process_archive.return_value = ("test-cluster-123", 5)
    return service


@pytest.fixture
def mock_session_factory():
    """Create a mock session factory."""
    session = Mock()
    factory = Mock(return_value=session)
    return factory


@pytest.fixture
def test_config():
    """Create test configuration."""
    return AppConfig(
        max_file_size=100 * 1024 * 1024,  # 100MB
        temp_upload_dir=tempfile.gettempdir(),
    )


@pytest.fixture
def upload_service(mock_processor_service, test_config, mock_session_factory):
    """Create UploadService instance with mocks."""
    return UploadService(
        processor_service=mock_processor_service,
        config=test_config,
        session_factory=mock_session_factory,
    )


@pytest.mark.asyncio
async def test_save_to_temp_success_tar_gz(upload_service):
    """Test saving .tar.gz file to temporary location."""
    test_data = b"test archive data"
    mock_file = Mock()
    mock_file.filename = "test.tar.gz"
    mock_file.read = AsyncMock(side_effect=[test_data, b""])

    temp_path, total_size = await upload_service.save_to_temp(mock_file, "req-123")

    assert os.path.exists(temp_path)
    assert temp_path.endswith(".tar.gz")
    assert total_size == len(test_data)

    # Verify file content
    with open(temp_path, "rb") as f:
        assert f.read() == test_data

    # Cleanup
    os.remove(temp_path)


@pytest.mark.asyncio
async def test_save_to_temp_success_tgz(upload_service):
    """Test saving .tgz file uses correct suffix."""
    test_data = b"test data"
    mock_file = Mock()
    mock_file.filename = "test.tgz"
    mock_file.read = AsyncMock(side_effect=[test_data, b""])

    temp_path, total_size = await upload_service.save_to_temp(mock_file, "req-123")

    assert temp_path.endswith(".tgz")
    assert os.path.exists(temp_path)

    # Cleanup
    os.remove(temp_path)


@pytest.mark.asyncio
async def test_save_to_temp_file_too_large(upload_service, test_config):
    """Test that files exceeding max size are rejected."""
    # Set small max size
    test_config.max_file_size = 100

    large_chunk = b"x" * 150
    mock_file = Mock()
    mock_file.filename = "test.tar.gz"
    mock_file.read = AsyncMock(side_effect=[large_chunk, b""])

    with pytest.raises(ValidationError, match="File size exceeds maximum"):
        await upload_service.save_to_temp(mock_file, "req-123")


@pytest.mark.asyncio
async def test_save_to_temp_chunks(upload_service):
    """Test saving file in multiple chunks."""
    chunk1 = b"chunk1"
    chunk2 = b"chunk2"
    chunk3 = b"chunk3"

    mock_file = Mock()
    mock_file.filename = "test.tar"
    mock_file.read = AsyncMock(side_effect=[chunk1, chunk2, chunk3, b""])

    temp_path, total_size = await upload_service.save_to_temp(mock_file, "req-123")

    assert total_size == len(chunk1) + len(chunk2) + len(chunk3)

    with open(temp_path, "rb") as f:
        assert f.read() == chunk1 + chunk2 + chunk3

    # Cleanup
    os.remove(temp_path)


@pytest.mark.asyncio
async def test_process_upload_success(upload_service):
    """Test successful upload scheduling."""
    test_data = b"test archive"
    mock_file = Mock()
    mock_file.filename = "test.tar.gz"
    mock_file.read = AsyncMock(side_effect=[test_data, b""])

    background_tasks = BackgroundTasks()

    result = await upload_service.process_upload(background_tasks, mock_file, "req-123")

    assert isinstance(result, UploadResponse)
    assert result.request_id == "req-123"
    assert result.status == "accepted"


@pytest.mark.asyncio
async def test_process_upload_schedules_background_task(upload_service):
    """Test that processing is scheduled as a background task."""
    test_data = b"test archive"
    mock_file = Mock()
    mock_file.filename = "test.tar.gz"
    mock_file.read = AsyncMock(side_effect=[test_data, b""])

    background_tasks = Mock(spec=BackgroundTasks)

    await upload_service.process_upload(background_tasks, mock_file, "req-123")

    background_tasks.add_task.assert_called_once()
    args = background_tasks.add_task.call_args
    assert args[0][0] == upload_service._process_in_background
    assert args[0][2] == "req-123"


@pytest.mark.asyncio
async def test_process_upload_validation_error(upload_service):
    """Test that validation errors are raised."""
    mock_file = Mock()
    mock_file.filename = "test.zip"  # Invalid format

    background_tasks = BackgroundTasks()

    with pytest.raises(ValidationError):
        await upload_service.process_upload(background_tasks, mock_file, "req-123")


def test_process_in_background_success(upload_service, mock_processor_service, mock_session_factory):
    """Test background processing calls processor and cleans up."""
    # Create a real temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as f:
        f.write(b"test data")
        temp_path = f.name

    upload_service._process_in_background(temp_path, "req-123")

    mock_processor_service.process_archive.assert_called_once()
    mock_session_factory.return_value.close.assert_called_once()
    assert not os.path.exists(temp_path)


def test_process_in_background_cleanup_on_error(upload_service, mock_processor_service, mock_session_factory):
    """Test background processing cleans up temp file even on failure."""
    mock_processor_service.process_archive.side_effect = Exception("Processing failed")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as f:
        f.write(b"test data")
        temp_path = f.name

    upload_service._process_in_background(temp_path, "req-123")

    mock_session_factory.return_value.close.assert_called_once()
    assert not os.path.exists(temp_path)
