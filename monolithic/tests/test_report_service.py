"""Tests for ReportService."""
import json
from datetime import datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Report, RuleHit
from app.services.report_service import ReportService
from app.schemas import ReportV2, ReportMetaV2


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def mock_content_service():
    """Create a mock ContentService."""
    service = Mock()
    service.get_content.return_value = {
        "description": "Test description",
        "generic": "Test generic",
        "reason": "Test reason",
        "resolution": "Test resolution",
        "more_info": "https://example.com",
        "total_risk": 2,
        "tags": ["test"],
        "publish_date": "2024-01-10T00:00:00Z",
    }
    return service


@pytest.fixture
def report_service(mock_content_service):
    """Create ReportService instance with mock content service."""
    return ReportService(content_service=mock_content_service)


def test_get_cluster_report_v2_not_found(db_session, report_service):
    """Test getting report for non-existent cluster raises ValueError."""
    with pytest.raises(ValueError, match="Cluster report not found"):
        report_service.get_cluster_report_v2(db_session, "nonexistent-cluster")


def test_get_cluster_report_v2_success(db_session, report_service, mock_content_service):
    """Test successfully getting a cluster report."""
    cluster_id = "test-cluster-123"

    # Create test report with insights data
    insights_results = {
        "reports": [
            {
                "component": "ccx_rules_ocp.external.rules.test_rule",
                "key": "TEST_ERROR",
                "details": {
                    "info": "Additional info",
                    "count": 3,
                }
            }
        ]
    }
    report_json = {
        "results": json.dumps(insights_results)
    }

    gathered_at = datetime(2024, 1, 15, 10, 0, 0)
    last_checked_at = datetime(2024, 1, 15, 11, 0, 0)

    report = Report(
        cluster=cluster_id,
        report=json.dumps(report_json),
        gathered_at=gathered_at,
        last_checked_at=last_checked_at,
    )
    db_session.add(report)

    # Create rule hit
    rule_hit = RuleHit(
        cluster_id=cluster_id,
        rule_fqdn="ccx_rules_ocp.external.rules.test_rule",
        error_key="TEST_ERROR",
        updated_at=datetime(2024, 1, 15, 10, 30, 0),
    )
    db_session.add(rule_hit)
    db_session.commit()

    # Get report
    result = report_service.get_cluster_report_v2(db_session, cluster_id)

    # Verify result structure
    assert isinstance(result, ReportV2)
    assert isinstance(result.meta, ReportMetaV2)
    assert result.meta.cluster_name == cluster_id
    assert result.meta.count == 1
    assert result.meta.managed is False
    assert len(result.data) == 1

    # Verify rule hit data
    hit_data = result.data[0]
    assert hit_data.rule_id == "ccx_rules_ocp.external.rules.test_rule"
    assert hit_data.description == "Test description"
    assert "TEST_ERROR" in hit_data.extra_data["error_key"]


def test_get_cluster_report_v2_no_rule_hits(db_session, report_service):
    """Test getting report with no rule hits."""
    cluster_id = "test-cluster-no-hits"

    report = Report(
        cluster=cluster_id,
        report='{"results": "{}"}',
        gathered_at=datetime(2024, 1, 15, 10, 0, 0),
        last_checked_at=datetime(2024, 1, 15, 11, 0, 0),
    )
    db_session.add(report)
    db_session.commit()

    result = report_service.get_cluster_report_v2(db_session, cluster_id)

    assert result.meta.count == 0
    assert len(result.data) == 0


def test_get_cluster_report_v2_invalid_json(db_session, report_service):
    """Test getting report with invalid JSON doesn't crash."""
    cluster_id = "test-cluster-bad-json"

    report = Report(
        cluster=cluster_id,
        report="invalid json {{{",
        gathered_at=datetime(2024, 1, 15, 10, 0, 0),
        last_checked_at=datetime(2024, 1, 15, 11, 0, 0),
    )
    db_session.add(report)
    db_session.commit()

    # Should not crash, just return empty results
    result = report_service.get_cluster_report_v2(db_session, cluster_id)

    assert result.meta.count == 0
    assert len(result.data) == 0


def test_get_cluster_report_v2_content_not_found(db_session, report_service, mock_content_service):
    """Test that rule hits without content are skipped."""
    cluster_id = "test-cluster-no-content"

    report = Report(
        cluster=cluster_id,
        report='{"results": "{}"}',
        gathered_at=datetime(2024, 1, 15, 10, 0, 0),
        last_checked_at=datetime(2024, 1, 15, 11, 0, 0),
    )
    db_session.add(report)

    # Create rule hit
    rule_hit = RuleHit(
        cluster_id=cluster_id,
        rule_fqdn="unknown.rule",
        error_key="UNKNOWN_ERROR",
        updated_at=datetime(2024, 1, 15, 10, 30, 0),
    )
    db_session.add(rule_hit)
    db_session.commit()

    # Mock content service to return None
    mock_content_service.get_content.return_value = None

    result = report_service.get_cluster_report_v2(db_session, cluster_id)

    # Rule hit should be skipped
    assert result.meta.count == 0
    assert len(result.data) == 0


def test_build_rule_hits_v2_normalizes_rule_fqdn(db_session, report_service, mock_content_service):
    """Test that .report suffix is stripped when looking up content."""
    cluster_id = "test-cluster-normalize"

    report = Report(
        cluster=cluster_id,
        report='{"results": "{}"}',
        gathered_at=datetime(2024, 1, 15, 10, 0, 0),
        last_checked_at=datetime(2024, 1, 15, 11, 0, 0),
    )
    db_session.add(report)

    # Create rule hit with .report suffix
    rule_hit = RuleHit(
        cluster_id=cluster_id,
        rule_fqdn="ccx_rules_ocp.external.rules.test_rule.report",
        error_key="TEST_ERROR",
        updated_at=datetime(2024, 1, 15, 10, 30, 0),
    )
    db_session.add(rule_hit)
    db_session.commit()

    result = report_service.get_cluster_report_v2(db_session, cluster_id)

    # Verify content service was called with normalized name
    mock_content_service.get_content.assert_called_with(
        "ccx_rules_ocp.external.rules.test_rule",
        "TEST_ERROR"
    )

    assert len(result.data) == 1


def test_get_cluster_report_v2_multiple_hits(db_session, report_service):
    """Test getting report with multiple rule hits."""
    cluster_id = "test-cluster-multi"

    report = Report(
        cluster=cluster_id,
        report='{"results": "{}"}',
        gathered_at=datetime(2024, 1, 15, 10, 0, 0),
        last_checked_at=datetime(2024, 1, 15, 11, 0, 0),
    )
    db_session.add(report)

    # Create multiple rule hits
    for i in range(5):
        rule_hit = RuleHit(
            cluster_id=cluster_id,
            rule_fqdn=f"rule_{i}",
            error_key=f"ERROR_{i}",
            updated_at=datetime(2024, 1, 15, 10, 30, 0),
        )
        db_session.add(rule_hit)
    db_session.commit()

    result = report_service.get_cluster_report_v2(db_session, cluster_id)

    assert result.meta.count == 5
    assert len(result.data) == 5


def test_get_cluster_report_v2_timestamps(db_session, report_service):
    """Test that timestamps are properly formatted in report metadata."""
    cluster_id = "test-cluster-timestamps"

    gathered_at = datetime(2024, 1, 15, 10, 30, 45)
    last_checked_at = datetime(2024, 1, 15, 11, 45, 30)

    report = Report(
        cluster=cluster_id,
        report='{"results": "{}"}',
        gathered_at=gathered_at,
        last_checked_at=last_checked_at,
    )
    db_session.add(report)
    db_session.commit()

    result = report_service.get_cluster_report_v2(db_session, cluster_id)

    assert result.meta.last_checked_at == "2024-01-15T11:45:30Z"
    assert result.meta.gathered_at == "2024-01-15T10:30:45Z"
