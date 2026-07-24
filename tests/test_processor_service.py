"""Tests for processor service."""

import json
from unittest.mock import MagicMock, Mock, patch

import pytest
from app.config import AppConfig
from app.exceptions import ProcessingError
from app.models import Report, RuleHit
from app.services.processor_service import ProcessorService

REQUEST_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def service_config(tmp_path):
    """Default processor service config using tmp_path."""
    return AppConfig(
        extract_timeout_seconds=300,
        temp_upload_dir=str(tmp_path),
        format="insights.formats._json.JsonFormat",
        target_components=[],
        unpacked_archive_size_limit=-1,
    )


@pytest.fixture
def processor_service(service_config):
    """Create processor service instance with test config."""
    return ProcessorService(service_config)


def test_init_with_valid_config(service_config, tmp_path):
    """Test initialization with valid config."""
    service = ProcessorService(service_config)

    assert service.extract_timeout_seconds == 300
    assert service.extract_tmp_dir == str(
        tmp_path
    )  # sourced from config.temp_upload_dir
    assert service.unpacked_archive_size_limit == -1


def test_init_with_custom_components(service_config):
    """Test initialization with custom target components."""
    service_config.target_components = ["component1", "component2"]
    service_config.unpacked_archive_size_limit = 1000000

    service = ProcessorService(service_config)

    assert service.unpacked_archive_size_limit == 1000000


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
    results_json = json.dumps(
        {
            "reports": [
                {
                    "component": "ccx_rules_ocp.external.rules.example_rule.report",
                    "key": "ERROR_KEY_1",
                    "type": "rule",
                    "details": {},
                },
                {
                    "component": "ccx_rules_ocp.external.rules.another_rule.report",
                    "key": "ERROR_KEY_2",
                    "type": "rule",
                    "details": {},
                },
            ]
        }
    )

    rule_hits = processor_service.extract_rule_hits(results_json)

    assert len(rule_hits) == 2
    assert (
        rule_hits[0]["rule_fqdn"] == "ccx_rules_ocp.external.rules.example_rule.report"
    )
    assert rule_hits[0]["error_key"] == "ERROR_KEY_1"
    assert (
        rule_hits[1]["rule_fqdn"] == "ccx_rules_ocp.external.rules.another_rule.report"
    )
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
        reports.append(
            {"component": rule_fqdn, "key": error_key, "type": "rule", "details": {}}
        )
    return json.dumps({"reports": reports})


def test_save_results_success(processor_service, database):
    """Test successful save of results."""
    cluster_id = "test-cluster-123"
    results_json = _make_results_json(
        [("ccx_rules_ocp.external.rules.example.report", "ERROR_KEY")]
    )

    count = processor_service.save_results(
        database, cluster_id, results_json, REQUEST_ID
    )

    assert count == 1

    # Verify report was saved
    report = database.query(Report).filter_by(cluster=cluster_id).first()
    assert report is not None

    # Verify rule hit was saved
    rule_hit = database.query(RuleHit).filter_by(cluster_id=cluster_id).first()
    assert rule_hit is not None
    assert rule_hit.rule_fqdn == "ccx_rules_ocp.external.rules.example.report"
    assert rule_hit.error_key == "ERROR_KEY"


def test_save_results_transaction_rollback_on_error(processor_service, database):
    """Test transaction rollback when save fails."""
    cluster_id = "test-cluster-123"
    results_json = _make_results_json(
        [("ccx_rules_ocp.external.rules.example.report", "ERROR_KEY")]
    )

    # Mock RuleHit.upsert to raise an error
    with (
        patch.object(RuleHit, "upsert", side_effect=Exception("Database error")),
        pytest.raises(ProcessingError, match="Database save failed"),
    ):
        processor_service.save_results(database, cluster_id, results_json, REQUEST_ID)

    # Verify nothing was committed
    report = database.query(Report).filter_by(cluster=cluster_id).first()
    assert report is None

    rule_hit = database.query(RuleHit).filter_by(cluster_id=cluster_id).first()
    assert rule_hit is None


def test_save_results_replaces_old_rule_hits(processor_service, database):
    """Test that old rule hits are replaced with new ones."""
    cluster_id = "test-cluster-123"

    # Save initial rule hits
    RuleHit.upsert(database, cluster_id, "old_rule", "OLD_KEY")
    database.commit()

    # Verify old rule exists
    old_hits = database.query(RuleHit).filter_by(cluster_id=cluster_id).all()
    assert len(old_hits) == 1

    # Save new results with different rules
    results_json = _make_results_json([("new_rule", "NEW_KEY")])

    processor_service.save_results(database, cluster_id, results_json, REQUEST_ID)

    # Verify only new rule exists
    new_hits = database.query(RuleHit).filter_by(cluster_id=cluster_id).all()
    assert len(new_hits) == 1
    assert new_hits[0].rule_fqdn == "new_rule"
    assert new_hits[0].error_key == "NEW_KEY"


def test_save_results_upserts_report_on_reprocessing(processor_service, database):
    """Test that reprocessing a cluster updates the existing report, not duplicates it."""
    cluster_id = "test-cluster-123"
    first_results = _make_results_json([("rule_a", "ERR_A")])
    second_results = _make_results_json([("rule_b", "ERR_B")])

    processor_service.save_results(database, cluster_id, first_results, REQUEST_ID)
    processor_service.save_results(
        database, cluster_id, second_results, "second-request"
    )

    reports = database.query(Report).filter_by(cluster=cluster_id).all()
    assert len(reports) == 1

    report_data = json.loads(reports[0].report)
    assert report_data["results"] == second_results


def test_save_results_empty_rule_hits(processor_service, database):
    """Test saving results with no rule hits."""
    cluster_id = "test-cluster-123"
    results_json = json.dumps({"reports": []})

    count = processor_service.save_results(
        database, cluster_id, results_json, REQUEST_ID
    )

    assert count == 0

    # Verify report was still saved
    report = database.query(Report).filter_by(cluster=cluster_id).first()
    assert report is not None

    # Verify no rule hits
    rule_hits = database.query(RuleHit).filter_by(cluster_id=cluster_id).all()
    assert len(rule_hits) == 0


@patch("app.services.processor_service.extract")
@patch("app.services.processor_service.initialize_broker")
@patch("app.services.processor_service.dr")
def test_process_archive_success(
    mock_dr, mock_init_broker, mock_extract, processor_service, database, tmp_path
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

    with patch("app.services.processor_service.StringIO") as mock_stringio:
        mock_output = MagicMock()
        mock_output.read.return_value = test_results
        mock_stringio.return_value = mock_output

        cluster_id, count = processor_service.process_archive(
            database, "/fake/archive.tar.gz", REQUEST_ID
        )

    assert cluster_id == "test-cluster-123"
    assert count == 1


@patch("app.services.processor_service.extract")
def test_process_archive_extraction_fails(mock_extract, processor_service, database):
    """Test archive processing when extraction fails."""
    mock_extract.side_effect = Exception("Extraction failed")

    with pytest.raises(ProcessingError, match="Analysis failed"):
        processor_service.process_archive(database, "/fake/archive.tar.gz", REQUEST_ID)


@patch("app.services.processor_service.extract")
def test_process_archive_size_limit_exceeded(mock_extract, service_config, tmp_path):
    """Test archive processing when size limit is exceeded."""
    service_config.unpacked_archive_size_limit = 100
    service = ProcessorService(service_config)

    mock_extraction = MagicMock()
    mock_extraction.tmp_dir = str(tmp_path / "extraction")
    mock_extract.return_value.__enter__.return_value = mock_extraction

    with (
        patch.object(service, "_validate_size", return_value=False),
        pytest.raises(ProcessingError, match="Archive exceeds size limit"),
    ):
        service.process_archive(Mock(), "/fake/archive.tar.gz", REQUEST_ID)
