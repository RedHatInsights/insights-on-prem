"""Tests for database models."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Report, RuleHit


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


def test_report_upsert_new(db_session):
    """Test inserting a new report."""
    cluster_id = "test-cluster-123"
    report_data = '{"results": "{}"}'
    gathered_at = datetime(2024, 1, 15, 10, 0, 0)

    report = Report.upsert(
        db=db_session,
        cluster=cluster_id,
        report=report_data,
        gathered_at=gathered_at,
    )

    db_session.commit()

    assert report.cluster == cluster_id
    assert report.report == report_data
    assert report.gathered_at == gathered_at
    assert report.reported_at is not None
    assert report.last_checked_at is not None


def test_report_upsert_existing(db_session):
    """Test updating an existing report."""
    cluster_id = "test-cluster-456"
    original_report = '{"results": "original"}'
    updated_report = '{"results": "updated"}'

    # Insert original
    Report.upsert(
        db=db_session,
        cluster=cluster_id,
        report=original_report,
    )
    db_session.commit()

    # Update
    updated = Report.upsert(
        db=db_session,
        cluster=cluster_id,
        report=updated_report,
    )
    db_session.commit()

    # Verify
    assert updated.report == updated_report
    assert updated.cluster == cluster_id

    # Verify only one record exists
    count = db_session.query(Report).filter_by(cluster=cluster_id).count()
    assert count == 1


def test_report_timestamps(db_session):
    """Test that timestamps are set correctly."""
    cluster_id = "test-cluster-timestamps"
    report_data = '{"results": "{}"}'

    report = Report.upsert(
        db=db_session,
        cluster=cluster_id,
        report=report_data,
    )
    db_session.commit()

    assert isinstance(report.reported_at, datetime)
    assert isinstance(report.last_checked_at, datetime)
    assert isinstance(report.gathered_at, datetime)


def test_rule_hit_upsert_new(db_session):
    """Test inserting a new rule hit."""
    cluster_id = "test-cluster-123"
    rule_fqdn = "ccx_rules_ocp.external.rules.test_rule"
    error_key = "TEST_ERROR"

    hit = RuleHit.upsert(
        db=db_session,
        cluster_id=cluster_id,
        rule_fqdn=rule_fqdn,
        error_key=error_key,
    )

    db_session.commit()

    assert hit.cluster_id == cluster_id
    assert hit.rule_fqdn == rule_fqdn
    assert hit.error_key == error_key
    assert isinstance(hit.updated_at, datetime)


def test_rule_hit_upsert_existing(db_session):
    """Test updating an existing rule hit updates timestamp."""
    cluster_id = "test-cluster-456"
    rule_fqdn = "test.rule"
    error_key = "ERROR"

    # Insert original
    original = RuleHit.upsert(
        db=db_session,
        cluster_id=cluster_id,
        rule_fqdn=rule_fqdn,
        error_key=error_key,
    )
    db_session.commit()
    original_timestamp = original.updated_at

    # Small delay to ensure different timestamp
    import time
    time.sleep(0.01)

    # Update
    updated = RuleHit.upsert(
        db=db_session,
        cluster_id=cluster_id,
        rule_fqdn=rule_fqdn,
        error_key=error_key,
    )
    db_session.commit()

    # Verify only one record exists
    count = (
        db_session.query(RuleHit)
        .filter_by(
            cluster_id=cluster_id,
            rule_fqdn=rule_fqdn,
            error_key=error_key,
        )
        .count()
    )
    assert count == 1

    # Timestamp should be updated
    assert updated.updated_at >= original_timestamp


def test_rule_hit_delete_for_cluster(db_session):
    """Test deleting all rule hits for a cluster."""
    cluster_id = "test-cluster-delete"

    # Insert multiple rule hits
    RuleHit.upsert(db=db_session, cluster_id=cluster_id, rule_fqdn="rule1", error_key="ERROR1")
    RuleHit.upsert(db=db_session, cluster_id=cluster_id, rule_fqdn="rule2", error_key="ERROR2")
    RuleHit.upsert(db=db_session, cluster_id=cluster_id, rule_fqdn="rule3", error_key="ERROR3")
    db_session.commit()

    # Verify they exist
    count_before = db_session.query(RuleHit).filter_by(cluster_id=cluster_id).count()
    assert count_before == 3

    # Delete all for cluster
    deleted_count = RuleHit.delete_for_cluster(db=db_session, cluster_id=cluster_id)
    db_session.commit()

    assert deleted_count == 3

    # Verify they're deleted
    count_after = db_session.query(RuleHit).filter_by(cluster_id=cluster_id).count()
    assert count_after == 0


def test_rule_hit_delete_for_cluster_no_hits(db_session):
    """Test deleting for cluster with no hits returns 0."""
    deleted_count = RuleHit.delete_for_cluster(db=db_session, cluster_id="nonexistent")
    db_session.commit()

    assert deleted_count == 0


def test_rule_hit_composite_primary_key(db_session):
    """Test that composite primary key works correctly."""
    cluster_id = "test-cluster"

    # Insert two different error keys for same rule
    RuleHit.upsert(db=db_session, cluster_id=cluster_id, rule_fqdn="rule1", error_key="ERROR1")
    RuleHit.upsert(db=db_session, cluster_id=cluster_id, rule_fqdn="rule1", error_key="ERROR2")

    # Insert same rule for different cluster
    RuleHit.upsert(db=db_session, cluster_id="other-cluster", rule_fqdn="rule1", error_key="ERROR1")

    db_session.commit()

    # Should have 3 distinct records
    total_count = db_session.query(RuleHit).count()
    assert total_count == 3
