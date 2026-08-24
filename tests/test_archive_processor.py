"""Tests for ArchiveProcessor."""

import os
import tempfile
from queue import Full
from unittest.mock import Mock

import pytest

from app.services.archive_processor import ArchiveProcessor


def _make_processor(process_archive=None, queue=None, queue_size=10):
    processor_service = Mock()
    processor_service.process_archive.side_effect = process_archive
    if process_archive is None:
        processor_service.process_archive.return_value = ("test-cluster-123", 5)

    session = Mock()
    session_factory = Mock(return_value=session)

    processor = ArchiveProcessor(
        processor_service,
        session_factory,
        queue_size=queue_size,
        queue=queue,
    )
    return processor, processor_service, session_factory, processor.queue


def _temp_archive():
    with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as f:
        f.write(b"test data")
        return f.name


def test_process_job_success():
    """Test processing calls processor and cleans up the temp file."""
    processor, processor_service, session_factory, _queue = _make_processor()
    temp_path = _temp_archive()

    processor._process_job(temp_path, "req-123")

    processor_service.process_archive.assert_called_once()
    session_factory.return_value.close.assert_called_once()
    assert not os.path.exists(temp_path)


def test_process_job_cleanup_on_error():
    """Test processing cleans up temp file even on failure."""
    processor, processor_service, session_factory, _queue = _make_processor()
    processor_service.process_archive.side_effect = Exception("Processing failed")
    temp_path = _temp_archive()

    processor._process_job(temp_path, "req-123")

    session_factory.return_value.close.assert_called_once()
    assert not os.path.exists(temp_path)


def test_processor_creates_bounded_queue():
    """Test the processor creates a queue with the requested max size."""
    processor, _service, _factory, queue = _make_processor(queue_size=2)

    assert queue.maxsize == 2
    queue.put_nowait(("a", "req-1"))
    queue.put_nowait(("b", "req-2"))
    with pytest.raises(Full):
        queue.put_nowait(("c", "req-3"))


def test_processor_rejects_invalid_queue_size():
    """Test queue_size must be at least 1."""
    with pytest.raises(ValueError, match="queue_size"):
        ArchiveProcessor(Mock(), Mock(), queue_size=0)


def test_processor_consumes_queue_on_single_thread():
    """Test the worker thread consumes queued jobs sequentially then stops."""
    processed = []

    def process_archive(_db, _path, request_id):
        processed.append(request_id)
        return ("cluster", 1)

    processor, _service, _factory, queue = _make_processor(process_archive)
    temp_path_1 = _temp_archive()
    temp_path_2 = _temp_archive()

    processor.start()
    try:
        queue.put((temp_path_1, "req-1"))
        queue.put((temp_path_2, "req-2"))
        stopped = processor.stop(timeout=5)
    except Exception:
        processor.stop(timeout=5)
        raise

    assert stopped
    assert processed == ["req-1", "req-2"]
    assert not os.path.exists(temp_path_1)
    assert not os.path.exists(temp_path_2)


def test_stop_on_never_started_thread():
    """Test stop is a no-op if the thread was never started."""
    processor, _service, _factory, _queue = _make_processor()
    assert processor.stop(timeout=1)
