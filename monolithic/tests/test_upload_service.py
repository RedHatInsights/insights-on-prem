"""Tests for UploadService."""
import os
import tempfile
from io import BytesIO
from unittest.mock import Mock, AsyncMock

import pytest

from app.config import Settings
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
def test_settings():
    """Create test settings."""
    settings = Mock(spec=Settings)
    settings.max_file_size = 100 * 1024 * 1024  # 100MB
    settings.temp_upload_dir = tempfile.gettempdir()
    return settings


@pytest.fixture
def upload_service(mock_processor_service, test_settings):
    """Create UploadService instance with mocks."""
    return UploadService(
        processor_service=mock_processor_service,
        settings=test_settings
    )


def test_validate_file_success_tar_gz(upload_service):
    """Test validating a valid .tar.gz file."""
    mock_file = Mock()
    mock_file.filename = "test.tar.gz"

    # Should not raise
    upload_service.validate_file(mock_file, "req-123")


def test_validate_file_success_tgz(upload_service):
    """Test validating a valid .tgz file."""
    mock_file = Mock()
    mock_file.filename = "test.tgz"

    upload_service.validate_file(mock_file, "req-123")


def test_validate_file_success_tar(upload_service):
    """Test validating a valid .tar file."""
    mock_file = Mock()
    mock_file.filename = "test.tar"

    upload_service.validate_file(mock_file, "req-123")


def test_validate_file_no_filename(upload_service):
    """Test validation fails when no filename provided."""
    mock_file = Mock()
    mock_file.filename = None

    with pytest.raises(ValidationError, match="No filename provided"):
        upload_service.validate_file(mock_file, "req-123")


def test_validate_file_empty_filename(upload_service):
    """Test validation fails when filename is empty."""
    mock_file = Mock()
    mock_file.filename = ""

    with pytest.raises(ValidationError, match="No filename provided"):
        upload_service.validate_file(mock_file, "req-123")


def test_validate_file_invalid_format(upload_service):
    """Test validation fails for invalid file format."""
    mock_file = Mock()
    mock_file.filename = "test.zip"

    with pytest.raises(ValidationError, match="File must be a .tar"):
        upload_service.validate_file(mock_file, "req-123")


def test_validate_file_txt_format(upload_service):
    """Test validation fails for .txt file."""
    mock_file = Mock()
    mock_file.filename = "test.txt"

    with pytest.raises(ValidationError, match="File must be a .tar"):
        upload_service.validate_file(mock_file, "req-123")


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
async def test_save_to_temp_file_too_large(upload_service, test_settings):
    """Test that files exceeding max size are rejected."""
    # Set small max size
    test_settings.max_file_size = 100

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
async def test_process_upload_success(upload_service, mock_processor_service):
    """Test successful upload processing."""
    test_data = b"test archive"
    mock_file = Mock()
    mock_file.filename = "test.tar.gz"
    mock_file.read = AsyncMock(side_effect=[test_data, b""])

    mock_db = Mock()
    request_id = "req-123"

    result = await upload_service.process_upload(mock_db, mock_file, request_id)

    # Verify result
    assert isinstance(result, UploadResponse)
    assert result.request_id == request_id
    assert result.status == "processed"
    assert result.cluster_id == "test-cluster-123"
    assert result.rules_found == 5

    # Verify processor was called
    mock_processor_service.process_archive.assert_called_once()


@pytest.mark.asyncio
async def test_process_upload_validation_error(upload_service):
    """Test that validation errors are raised."""
    mock_file = Mock()
    mock_file.filename = "test.zip"  # Invalid format

    mock_db = Mock()

    with pytest.raises(ValidationError):
        await upload_service.process_upload(mock_db, mock_file, "req-123")


@pytest.mark.asyncio
async def test_process_upload_cleanup_on_success(upload_service):
    """Test that temporary file is cleaned up after successful processing."""
    test_data = b"test archive"
    mock_file = Mock()
    mock_file.filename = "test.tar.gz"
    mock_file.read = AsyncMock(side_effect=[test_data, b""])

    mock_db = Mock()

    # Track temp file path
    temp_path_holder = []
    original_save = upload_service.save_to_temp

    async def track_temp_path(*args, **kwargs):
        path, size = await original_save(*args, **kwargs)
        temp_path_holder.append(path)
        return path, size

    upload_service.save_to_temp = track_temp_path

    result = await upload_service.process_upload(mock_db, mock_file, "req-123")

    # Verify temp file was deleted
    assert len(temp_path_holder) == 1
    assert not os.path.exists(temp_path_holder[0])


@pytest.mark.asyncio
async def test_process_upload_cleanup_on_error(upload_service, mock_processor_service):
    """Test that temporary file is cleaned up even when processing fails."""
    test_data = b"test archive"
    mock_file = Mock()
    mock_file.filename = "test.tar.gz"
    mock_file.read = AsyncMock(side_effect=[test_data, b""])

    # Make processor raise exception
    mock_processor_service.process_archive.side_effect = Exception("Processing failed")

    mock_db = Mock()

    temp_path_holder = []
    original_save = upload_service.save_to_temp

    async def track_temp_path(*args, **kwargs):
        path, size = await original_save(*args, **kwargs)
        temp_path_holder.append(path)
        return path, size

    upload_service.save_to_temp = track_temp_path

    with pytest.raises(Exception, match="Processing failed"):
        await upload_service.process_upload(mock_db, mock_file, "req-123")

    # Verify temp file was still deleted
    assert len(temp_path_holder) == 1
    assert not os.path.exists(temp_path_holder[0])
