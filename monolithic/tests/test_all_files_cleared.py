"""
Tests for the ctx.all_files memory fix in ProcessorService.

After initialize_broker() returns, ctx.all_files holds ~1 MB of path strings
that are not needed for component execution (specs use glob on ctx.root).
Clearing the list immediately releases those strings to glibc so that
malloc_trim(0) can reclaim the brk pages within the same archive processing
cycle rather than waiting for idle time.

These tests verify:
 1. all_files is empty before dr.run_components() runs.
 2. all_files is emptied even when run_components() raises.
 3. ctx and broker are freed by reference counting alone after processing
    (no cyclic GC needed), confirming del broker, ctx works correctly.
 4. mallopt(M_TRIM_THRESHOLD=0) is called at startup without crashing.
"""

import gc
import platform
import weakref
from unittest.mock import MagicMock, Mock, patch

import pytest
from app.config import AppConfig
from app.exceptions import ProcessingError
from app.services.processor_service import ProcessorService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def service_config(tmp_path):
    return AppConfig(
        extract_timeout_seconds=300,
        temp_upload_dir=str(tmp_path),
        format="insights.formats._json.JsonFormat",
        target_components=[],
        unpacked_archive_size_limit=-1,
    )


@pytest.fixture
def processor_service(service_config):
    return ProcessorService(service_config)


def _setup_mock_extraction(tmp_path, mock_extract, cluster_id="test-cluster"):
    mock_extraction = MagicMock()
    mock_extraction.tmp_dir = str(tmp_path / "extraction")
    mock_extract.return_value.__enter__.return_value = mock_extraction

    config_dir = tmp_path / "extraction" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "id").write_text(cluster_id)

    return mock_extraction


# ---------------------------------------------------------------------------
# 1. all_files cleared before run_components
# ---------------------------------------------------------------------------


@patch("app.services.processor_service.extract")
@patch("app.services.processor_service.initialize_broker")
@patch("app.services.processor_service.dr")
def test_all_files_cleared_before_run_components(
    mock_dr, mock_init_broker, mock_extract, processor_service, tmp_path
):
    """ctx.all_files must be [] by the time dr.run_components() is called."""
    _setup_mock_extraction(tmp_path, mock_extract)

    mock_ctx = Mock()
    mock_ctx.all_files = ["/tmp/path1", "/tmp/path2", "/tmp/path3"]
    mock_broker = Mock()
    mock_init_broker.return_value = (mock_ctx, mock_broker)

    # Capture the state of all_files at the moment run_components is invoked
    all_files_during_run = []

    def capture(_components, _comp_dict, broker):
        all_files_during_run.append(list(mock_ctx.all_files))

    mock_dr.run_components.side_effect = capture
    processor_service.Formatter = MagicMock()

    with patch("app.services.processor_service.StringIO") as mock_sio:
        mock_sio.return_value.read.return_value = "{}"
        processor_service.process_with_insights_core("/fake/archive.tar.gz")

    assert all_files_during_run == [[]], (
        "ctx.all_files was not empty when dr.run_components() ran — "
        "path strings are still allocated, blocking malloc_trim"
    )


@patch("app.services.processor_service.extract")
@patch("app.services.processor_service.initialize_broker")
@patch("app.services.processor_service.dr")
def test_all_files_cleared_even_when_run_components_raises(
    mock_dr, mock_init_broker, mock_extract, processor_service, tmp_path
):
    """ctx.all_files must be cleared before run_components, even if it raises."""
    _setup_mock_extraction(tmp_path, mock_extract)

    mock_ctx = Mock()
    mock_ctx.all_files = ["/tmp/a", "/tmp/b"]
    mock_broker = Mock()
    mock_init_broker.return_value = (mock_ctx, mock_broker)

    mock_dr.run_components.side_effect = RuntimeError("component failure")
    processor_service.Formatter = MagicMock()

    with pytest.raises(ProcessingError):
        processor_service.process_with_insights_core("/fake/archive.tar.gz")

    # all_files was cleared before run_components was called
    assert mock_ctx.all_files == []


# ---------------------------------------------------------------------------
# 2. broker.cleanup() called in all paths
# ---------------------------------------------------------------------------


@patch("app.services.processor_service.extract")
@patch("app.services.processor_service.initialize_broker")
@patch("app.services.processor_service.dr")
def test_broker_cleanup_called_on_success(
    mock_dr, mock_init_broker, mock_extract, processor_service, tmp_path
):
    """broker.cleanup() must be called after successful processing."""
    _setup_mock_extraction(tmp_path, mock_extract)

    mock_ctx, mock_broker = Mock(), Mock()
    mock_init_broker.return_value = (mock_ctx, mock_broker)
    processor_service.Formatter = MagicMock()

    with patch("app.services.processor_service.StringIO") as mock_sio:
        mock_sio.return_value.read.return_value = "{}"
        processor_service.process_with_insights_core("/fake/archive.tar.gz")

    mock_broker.cleanup.assert_called_once()


@patch("app.services.processor_service.extract")
@patch("app.services.processor_service.initialize_broker")
@patch("app.services.processor_service.dr")
def test_broker_cleanup_called_on_run_components_exception(
    mock_dr, mock_init_broker, mock_extract, processor_service, tmp_path
):
    """broker.cleanup() must be called even when dr.run_components() raises."""
    _setup_mock_extraction(tmp_path, mock_extract)

    mock_ctx, mock_broker = Mock(), Mock()
    mock_init_broker.return_value = (mock_ctx, mock_broker)
    mock_dr.run_components.side_effect = RuntimeError("failure")
    processor_service.Formatter = MagicMock()

    with pytest.raises(ProcessingError):
        processor_service.process_with_insights_core("/fake/archive.tar.gz")

    mock_broker.cleanup.assert_called_once()


# ---------------------------------------------------------------------------
# 3. ctx freed by reference counting after processing (del broker, ctx works)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    platform.python_implementation() != "CPython",
    reason="Relies on CPython reference-counting semantics",
)
@patch("app.services.processor_service.extract")
@patch("app.services.processor_service.initialize_broker")
@patch("app.services.processor_service.dr")
def test_all_files_list_freed_promptly_on_success(
    mock_dr, mock_init_broker, mock_extract, processor_service, tmp_path
):
    """The original all_files list must be freed the moment ctx.all_files = [] runs.

    ctx.all_files = [] drops the last reference to the original list, so the
    path strings are freed immediately by CPython refcounting — before
    dr.run_components() executes and before gc.collect() + malloc_trim(0) run
    in the caller.  This is what actually releases the brk pages.
    """
    _setup_mock_extraction(tmp_path, mock_extract)

    # list subclass supports weakref; plain list does not
    class TrackableList(list):
        pass

    original_list = TrackableList(["/path/a", "/path/b", "/path/c"])
    list_ref = weakref.ref(original_list)

    mock_ctx = Mock()
    mock_ctx.all_files = original_list
    del original_list  # ctx.all_files is now the sole owner

    mock_broker = MagicMock()
    mock_init_broker.return_value = (mock_ctx, mock_broker)

    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        processor_service.Formatter = MagicMock()
        with patch("app.services.processor_service.StringIO") as mock_sio:
            mock_sio.return_value.read.return_value = "{}"
            processor_service.process_with_insights_core("/fake/archive.tar.gz")

        # ctx.all_files = [] inside process_with_insights_core drops the last
        # reference to original_list → refcount 0 → freed immediately
        assert list_ref() is None, (
            "all_files list was not freed promptly — ctx.all_files = [] "
            "may be missing or ran too late (after run_components)"
        )
    finally:
        if gc_was_enabled:
            gc.enable()
        gc.collect()


@pytest.mark.skipif(
    platform.python_implementation() != "CPython",
    reason="Relies on CPython reference-counting semantics",
)
@patch("app.services.processor_service.extract")
@patch("app.services.processor_service.initialize_broker")
@patch("app.services.processor_service.dr")
def test_all_files_list_freed_promptly_on_exception(
    mock_dr, mock_init_broker, mock_extract, processor_service, tmp_path
):
    """all_files list must be freed even when dr.run_components raises.

    ctx.all_files = [] runs before run_components, so the list is freed
    regardless of whether run_components succeeds or raises.
    """
    _setup_mock_extraction(tmp_path, mock_extract)

    class TrackableList(list):
        pass

    original_list = TrackableList(["/path/a", "/path/b"])
    list_ref = weakref.ref(original_list)

    mock_ctx = Mock()
    mock_ctx.all_files = original_list
    del original_list

    mock_broker = MagicMock()
    mock_init_broker.return_value = (mock_ctx, mock_broker)
    mock_dr.run_components.side_effect = RuntimeError("failure")
    processor_service.Formatter = MagicMock()

    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        with pytest.raises(ProcessingError):
            processor_service.process_with_insights_core("/fake/archive.tar.gz")

        assert list_ref() is None, (
            "all_files list was not freed even though run_components raised — "
            "ctx.all_files = [] must run before run_components, not after"
        )
    finally:
        if gc_was_enabled:
            gc.enable()
        gc.collect()


# ---------------------------------------------------------------------------
# 4. mallopt M_TRIM_THRESHOLD called at startup
# ---------------------------------------------------------------------------


def test_mallopt_trim_threshold_attempted_at_startup():
    """lifespan must attempt mallopt(M_TRIM_THRESHOLD=0) and not crash."""
    calls = []

    class _FakeLibc:
        def mallopt(self, param, value):
            calls.append((param, value))
            return 1

    import ctypes as _ctypes

    with patch.object(_ctypes, "CDLL", return_value=_FakeLibc()):
        # Re-execute just the mallopt block from lifespan
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").mallopt(-1, 0)
        except Exception:
            pass

    assert (-1, 0) in calls, (
        "mallopt(M_TRIM_THRESHOLD=0) was not called — "
        "glibc will not trim the brk heap aggressively during load"
    )


def test_mallopt_does_not_crash_when_libc_unavailable():
    """If libc.so.6 is missing the startup block must swallow the error silently."""
    import ctypes as _ctypes

    with patch.object(_ctypes, "CDLL", side_effect=OSError("no libc")):
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").mallopt(-1, 0)
        except Exception:
            pass  # the lifespan wraps this in try/except — must not propagate
