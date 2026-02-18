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

CLUSTER_ID = "test-cluster-123"
RULE_FQDN = "ccx_rules_ocp.external.rules.test_rule"
ERROR_KEY = "TEST_ERROR"
GATHERED_AT = datetime(2024, 1, 15, 10, 0, 0)
LAST_CHECKED_AT = datetime(2024, 1, 15, 11, 0, 0)
UPDATED_AT = datetime(2024, 1, 15, 10, 30, 0)


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
    insights_results = {
        "reports": [
            {
                "component": RULE_FQDN,
                "key": ERROR_KEY,
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

    report = Report(
        cluster=CLUSTER_ID,
        report=json.dumps(report_json),
        gathered_at=GATHERED_AT,
        last_checked_at=LAST_CHECKED_AT,
    )
    db_session.add(report)

    rule_hit = RuleHit(
        cluster_id=CLUSTER_ID,
        rule_fqdn=RULE_FQDN,
        error_key=ERROR_KEY,
        updated_at=UPDATED_AT,
    )
    db_session.add(rule_hit)
    db_session.commit()

    result = report_service.get_cluster_report_v2(db_session, CLUSTER_ID)

    assert isinstance(result, ReportV2)
    assert isinstance(result.meta, ReportMetaV2)
    assert result.meta.cluster_name == CLUSTER_ID
    assert result.meta.count == 1
    assert result.meta.managed is False
    assert len(result.data) == 1

    hit_data = result.data[0]
    assert hit_data.rule_id == RULE_FQDN
    assert hit_data.description == "Test description"
    assert ERROR_KEY in hit_data.extra_data["error_key"]


def test_get_cluster_report_v2_no_rule_hits(db_session, report_service):
    """Test getting report with no rule hits."""
    report = Report(
        cluster=CLUSTER_ID,
        report='{"results": "{}"}',
        gathered_at=GATHERED_AT,
        last_checked_at=LAST_CHECKED_AT,
    )
    db_session.add(report)
    db_session.commit()

    result = report_service.get_cluster_report_v2(db_session, CLUSTER_ID)

    assert result.meta.count == 0
    assert len(result.data) == 0


def test_get_cluster_report_v2_invalid_json(db_session, report_service):
    """Test getting report with invalid JSON doesn't crash."""
    report = Report(
        cluster=CLUSTER_ID,
        report="invalid json {{{",
        gathered_at=GATHERED_AT,
        last_checked_at=LAST_CHECKED_AT,
    )
    db_session.add(report)
    db_session.commit()

    result = report_service.get_cluster_report_v2(db_session, CLUSTER_ID)

    assert result.meta.count == 0
    assert len(result.data) == 0


def test_get_cluster_report_v2_content_not_found(db_session, report_service, mock_content_service):
    """Test that rule hits without content are skipped."""
    report = Report(
        cluster=CLUSTER_ID,
        report='{"results": "{}"}',
        gathered_at=GATHERED_AT,
        last_checked_at=LAST_CHECKED_AT,
    )
    db_session.add(report)

    rule_hit = RuleHit(
        cluster_id=CLUSTER_ID,
        rule_fqdn="unknown.rule",
        error_key="UNKNOWN_ERROR",
        updated_at=UPDATED_AT,
    )
    db_session.add(rule_hit)
    db_session.commit()

    mock_content_service.get_content.return_value = None

    result = report_service.get_cluster_report_v2(db_session, CLUSTER_ID)

    assert result.meta.count == 0
    assert len(result.data) == 0


def test_build_rule_hits_v2_normalizes_rule_fqdn(db_session, report_service, mock_content_service):
    """Test that .report suffix is stripped when looking up content."""
    report = Report(
        cluster=CLUSTER_ID,
        report='{"results": "{}"}',
        gathered_at=GATHERED_AT,
        last_checked_at=LAST_CHECKED_AT,
    )
    db_session.add(report)

    rule_hit = RuleHit(
        cluster_id=CLUSTER_ID,
        rule_fqdn=RULE_FQDN + ".report",
        error_key=ERROR_KEY,
        updated_at=UPDATED_AT,
    )
    db_session.add(rule_hit)
    db_session.commit()

    result = report_service.get_cluster_report_v2(db_session, CLUSTER_ID)

    mock_content_service.get_content.assert_called_with(RULE_FQDN, ERROR_KEY)
    assert len(result.data) == 1


def test_get_cluster_report_v2_multiple_hits(db_session, report_service):
    """Test getting report with multiple rule hits."""
    report = Report(
        cluster=CLUSTER_ID,
        report='{"results": "{}"}',
        gathered_at=GATHERED_AT,
        last_checked_at=LAST_CHECKED_AT,
    )
    db_session.add(report)

    for i in range(5):
        rule_hit = RuleHit(
            cluster_id=CLUSTER_ID,
            rule_fqdn=f"rule_{i}",
            error_key=f"ERROR_{i}",
            updated_at=UPDATED_AT,
        )
        db_session.add(rule_hit)
    db_session.commit()

    result = report_service.get_cluster_report_v2(db_session, CLUSTER_ID)

    assert result.meta.count == 5
    assert len(result.data) == 5


def test_get_cluster_report_v2_timestamps(db_session, report_service):
    """Test that timestamps are properly formatted in report metadata."""
    report = Report(
        cluster=CLUSTER_ID,
        report='{"results": "{}"}',
        gathered_at=GATHERED_AT,
        last_checked_at=LAST_CHECKED_AT,
    )
    db_session.add(report)
    db_session.commit()

    result = report_service.get_cluster_report_v2(db_session, CLUSTER_ID)

    assert result.meta.last_checked_at == "2024-01-15T11:00:00Z"
    assert result.meta.gathered_at == "2024-01-15T10:00:00Z"
