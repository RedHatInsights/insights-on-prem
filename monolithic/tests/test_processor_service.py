"""Tests for processor service."""
import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from app.services.processor_service import ProcessorService
from app.models import Report, RuleHit
from app.exceptions import ProcessingError


@pytest.fixture
def processor_service():
    """Create processor service instance with test config."""
    config = {
        "service": {
            "extract_timeout": 300,
            "extract_tmp_dir": "/tmp/test",
            "format": "insights.formats._json.JsonFormat",
            "target_components": [],
            "unpacked_archive_size_limit": -1,
        }
    }
    return ProcessorService(config)


@pytest.fixture
def mock_extraction_context():
    """Create mock extraction context."""
    mock_ctx = MagicMock()
    mock_ctx.tmp_dir = "/tmp/test/archive"
    return mock_ctx


def test_init_with_valid_config():
    """Test initialization with valid config."""
    config = {
        "service": {
            "extract_timeout": 300,
            "extract_tmp_dir": "/tmp/test",
            "format": "insights.formats.text.HumanReadableFormat",
            "target_components": [],
            "unpacked_archive_size_limit": -1,
        }
    }
    service = ProcessorService(config)

    assert service.extract_timeout == 300
    assert service.extract_tmp_dir == "/tmp/test"
    assert service.unpacked_archive_size_limit == -1


def test_init_with_custom_components():
    """Test initialization with custom target components."""
    config = {
        "service": {
            "extract_timeout": 300,
            "extract_tmp_dir": "/tmp/test",
            "format": "insights.formats.text.HumanReadableFormat",
            "target_components": ["component1", "component2"],
            "unpacked_archive_size_limit": 1000000,
        }
    }
    service = ProcessorService(config)

    assert service.unpacked_archive_size_limit == 1000000


def test_validate_size_unlimited(processor_service, tmp_path):
    """Test validation with unlimited size."""
    # Create a test directory
    test_dir = tmp_path / "archive"
    test_dir.mkdir()
    (test_dir / "file.txt").write_text("test content")

    # Service has unlimited size (-1)
    assert processor_service.validate_size(str(test_dir)) is True


def test_validate_size_within_limit(tmp_path):
    """Test validation when size is within limit."""
    config = {
        "service": {
            "extract_timeout": 300,
            "extract_tmp_dir": "/tmp/test",
            "format": "insights.formats._json.JsonFormat",
            "target_components": [],
            "unpacked_archive_size_limit": 1000000,  # 1MB
        }
    }
    service = ProcessorService(config)

    # Create small test directory
    test_dir = tmp_path / "archive"
    test_dir.mkdir()
    (test_dir / "file.txt").write_text("small content")

    assert service.validate_size(str(test_dir)) is True


@patch('app.services.processor_service.Path')
def test_validate_size_exceeds_limit(mock_path, tmp_path):
    """Test validation when size exceeds limit."""
    config = {
        "service": {
            "extract_timeout": 300,
            "extract_tmp_dir": "/tmp/test",
            "format": "insights.formats._json.JsonFormat",
            "target_components": [],
            "unpacked_archive_size_limit": 100,  # 100 bytes
        }
    }
    service = ProcessorService(config)

    # Mock a large file
    mock_file = Mock()
    mock_file.is_file.return_value = True
    mock_file.stat.return_value.st_size = 200  # 200 bytes

    mock_dir = Mock()
    mock_dir.rglob.return_value = [mock_file]

    mock_path.return_value = mock_dir

    assert service.validate_size("/fake/dir") is False


def test_get_cluster_id_from_id_file(processor_service, tmp_path):
    """Test extracting cluster ID from id file."""
    # Create test directory with id file
    test_dir = tmp_path / "archive"
    test_dir.mkdir()
    config_dir = test_dir / "config"
    config_dir.mkdir()
    (config_dir / "id").write_text("test-cluster-123")

    cluster_id = processor_service.get_cluster_id(str(test_dir))
    assert cluster_id == "test-cluster-123"


def test_get_cluster_id_missing_config_dir(processor_service, tmp_path):
    """Test error when config directory doesn't exist."""
    test_dir = tmp_path / "archive"
    test_dir.mkdir()

    with pytest.raises(ProcessingError, match="Could not find cluster ID"):
        processor_service.get_cluster_id(str(test_dir))


def test_get_cluster_id_missing_id_file(processor_service, tmp_path):
    """Test error when id file doesn't exist."""
    test_dir = tmp_path / "archive"
    test_dir.mkdir()
    config_dir = test_dir / "config"
    config_dir.mkdir()

    with pytest.raises(ProcessingError, match="Could not find cluster ID"):
        processor_service.get_cluster_id(str(test_dir))


def test_extract_rule_hits_valid_json(processor_service):
    """Test extracting rule hits from valid JSON."""
    # Code expects reports as a list of dicts with "type", "component", "key" fields
    results_json = json.dumps({
        "reports": [
            {
                "component": "ccx_rules_ocp.external.rules.example_rule.report",
                "key": "ERROR_KEY_1",
                "type": "rule",
                "details": {}
            },
            {
                "component": "ccx_rules_ocp.external.rules.another_rule.report",
                "key": "ERROR_KEY_2",
                "type": "rule",
                "details": {}
            }
        ]
    })

    rule_hits = processor_service.extract_rule_hits(results_json)

    assert len(rule_hits) == 2
    assert rule_hits[0]["rule_fqdn"] == "ccx_rules_ocp.external.rules.example_rule.report"
    assert rule_hits[0]["error_key"] == "ERROR_KEY_1"
    assert rule_hits[1]["rule_fqdn"] == "ccx_rules_ocp.external.rules.another_rule.report"
    assert rule_hits[1]["error_key"] == "ERROR_KEY_2"


def test_extract_rule_hits_no_reports(processor_service):
    """Test extracting when no reports section exists."""
    results_json = json.dumps({})

    rule_hits = processor_service.extract_rule_hits(results_json)

    assert rule_hits == []


def test_extract_rule_hits_invalid_json(processor_service):
    """Test extracting from invalid JSON."""
    results_json = "invalid json"

    rule_hits = processor_service.extract_rule_hits(results_json)

    assert rule_hits == []


def _make_results_json(rules):
    """Helper to build results JSON in the format expected by extract_rule_hits."""
    reports = []
    for rule_fqdn, error_key in rules:
        reports.append({
            "component": rule_fqdn,
            "key": error_key,
            "type": "rule",
            "details": {}
        })
    return json.dumps({"reports": reports})


def test_save_results_success(processor_service, test_db):
    """Test successful save of results."""
    cluster_id = "test-cluster-123"
    results_json = _make_results_json([
        ("ccx_rules_ocp.external.rules.example.report", "ERROR_KEY")
    ])

    count = processor_service.save_results(test_db, cluster_id, results_json)

    assert count == 1

    # Verify report was saved
    report = test_db.query(Report).filter_by(cluster=cluster_id).first()
    assert report is not None

    # Verify rule hit was saved
    rule_hit = test_db.query(RuleHit).filter_by(cluster_id=cluster_id).first()
    assert rule_hit is not None
    assert rule_hit.rule_fqdn == "ccx_rules_ocp.external.rules.example.report"
    assert rule_hit.error_key == "ERROR_KEY"


def test_save_results_transaction_rollback_on_error(processor_service, test_db):
    """Test transaction rollback when save fails."""
    cluster_id = "test-cluster-123"
    results_json = _make_results_json([
        ("ccx_rules_ocp.external.rules.example.report", "ERROR_KEY")
    ])

    # Mock RuleHit.upsert to raise an error
    with patch.object(RuleHit, 'upsert', side_effect=Exception("Database error")):
        with pytest.raises(ProcessingError, match="Database save failed"):
            processor_service.save_results(test_db, cluster_id, results_json)

    # Verify nothing was committed
    report = test_db.query(Report).filter_by(cluster=cluster_id).first()
    assert report is None

    rule_hit = test_db.query(RuleHit).filter_by(cluster_id=cluster_id).first()
    assert rule_hit is None


def test_save_results_replaces_old_rule_hits(processor_service, test_db):
    """Test that old rule hits are replaced with new ones."""
    cluster_id = "test-cluster-123"

    # Save initial rule hits
    RuleHit.upsert(test_db, cluster_id, "old_rule", "OLD_KEY")
    test_db.commit()

    # Verify old rule exists
    old_hits = test_db.query(RuleHit).filter_by(cluster_id=cluster_id).all()
    assert len(old_hits) == 1

    # Save new results with different rules
    results_json = _make_results_json([("new_rule", "NEW_KEY")])

    processor_service.save_results(test_db, cluster_id, results_json)

    # Verify only new rule exists
    new_hits = test_db.query(RuleHit).filter_by(cluster_id=cluster_id).all()
    assert len(new_hits) == 1
    assert new_hits[0].rule_fqdn == "new_rule"
    assert new_hits[0].error_key == "NEW_KEY"


def test_save_results_empty_rule_hits(processor_service, test_db):
    """Test saving results with no rule hits."""
    cluster_id = "test-cluster-123"
    results_json = json.dumps({"reports": []})

    count = processor_service.save_results(test_db, cluster_id, results_json)

    assert count == 0

    # Verify report was still saved
    report = test_db.query(Report).filter_by(cluster=cluster_id).first()
    assert report is not None

    # Verify no rule hits
    rule_hits = test_db.query(RuleHit).filter_by(cluster_id=cluster_id).all()
    assert len(rule_hits) == 0


@patch('app.services.processor_service.extract')
@patch('app.services.processor_service.initialize_broker')
@patch('app.services.processor_service.dr')
def test_process_archive_success(
    mock_dr,
    mock_init_broker,
    mock_extract,
    processor_service,
    test_db,
    tmp_path
):
    """Test successful archive processing."""
    # Setup mocks
    mock_extraction = MagicMock()
    mock_extraction.tmp_dir = str(tmp_path / "extraction")
    mock_extract.return_value.__enter__.return_value = mock_extraction

    # Create test cluster ID file
    config_dir = tmp_path / "extraction" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "id").write_text("test-cluster-123")

    mock_ctx = Mock()
    mock_broker = Mock()
    mock_init_broker.return_value = (mock_ctx, mock_broker)

    # Mock insights-core results (list format expected by extract_rule_hits)
    test_results = _make_results_json([("test_rule", "TEST_ERROR")])

    # Mock the Formatter and StringIO
    mock_formatter = MagicMock()
    processor_service.Formatter = mock_formatter

    with patch('app.services.processor_service.StringIO') as mock_stringio:
        mock_output = MagicMock()
        mock_output.read.return_value = test_results
        mock_stringio.return_value = mock_output

        cluster_id, count = processor_service.process_archive(test_db, "/fake/archive.tar.gz")

    assert cluster_id == "test-cluster-123"
    assert count == 1


@patch('app.services.processor_service.extract')
def test_process_archive_extraction_fails(mock_extract, processor_service, test_db):
    """Test archive processing when extraction fails."""
    mock_extract.side_effect = Exception("Extraction failed")

    with pytest.raises(ProcessingError, match="Analysis failed"):
        processor_service.process_archive(test_db, "/fake/archive.tar.gz")


@patch('app.services.processor_service.extract')
def test_process_archive_size_limit_exceeded(mock_extract, tmp_path):
    """Test archive processing when size limit is exceeded."""
    config = {
        "service": {
            "extract_timeout": 300,
            "extract_tmp_dir": "/tmp/test",
            "format": "insights.formats._json.JsonFormat",
            "target_components": [],
            "unpacked_archive_size_limit": 100,  # Very small limit
        }
    }
    service = ProcessorService(config)

    mock_extraction = MagicMock()
    mock_extraction.tmp_dir = str(tmp_path / "extraction")
    mock_extract.return_value.__enter__.return_value = mock_extraction

    with patch.object(service, 'validate_size', return_value=False):
        with pytest.raises(ProcessingError, match="Archive exceeds size limit"):
            service.process_archive(Mock(), "/fake/archive.tar.gz")
