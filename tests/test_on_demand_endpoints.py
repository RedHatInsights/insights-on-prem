"""Tests for on-demand gathering endpoints."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from app.main import app
from app.models import RequestReport
from fastapi.testclient import TestClient

client = TestClient(app)

REQUEST_ID = "00000000-0000-0000-0000-000000000000"
OLD_REQUEST_ID = "11111111-1111-1111-1111-111111111111"
CLUSTER_ID = "00000000-0000-0000-0000-000000000001"


def test_request_status_processed(database):
    """Test status endpoint returns processed for existing request."""
    RequestReport.create(
        db=database,
        request_id=REQUEST_ID,
        cluster_id=CLUSTER_ID,
        report="[]",
    )
    database.commit()

    response = client.get(f"/api/v2/cluster/{CLUSTER_ID}/request/{REQUEST_ID}/status")
    assert response.status_code == 200
    data = response.json()
    assert data["cluster"] == CLUSTER_ID
    assert data["requestID"] == REQUEST_ID
    assert data["status"] == "processed"


def test_request_report_returns_simplified_report(database):
    """Test report endpoint enriches rule hits with content (description, total_risk)."""

    # Rules with description/total_risk have content,
    # rules without them simulate missing content (skipped in response)
    test_rules = [
        {
            "rule_fqdn": "ccx_rules_ocp.external.rules.rule_a",
            "error_key": "ERR1",
            "description": "Rule A fires when ...",
            "total_risk": 3,
        },
        {
            "rule_fqdn": "ccx_rules_ocp.external.rules.rule_b",
            "error_key": "ERR2",
            "description": "Rule B detects ...",
            "total_risk": 1,
        },
        {
            "rule_fqdn": "ccx_rules_ocp.external.rules.unknown",
            "error_key": "ERR3",
        },
    ]

    # Build stored report — rule_fqdn is normalized at write time by save_results
    rule_hits = [
        {"rule_fqdn": r["rule_fqdn"], "error_key": r["error_key"], "details": {}}
        for r in test_rules
    ]
    RequestReport.create(
        db=database,
        request_id=REQUEST_ID,
        cluster_id=CLUSTER_ID,
        report=json.dumps(rule_hits),
    )
    database.commit()

    # Mock content service — return content only for rules that have it
    mock_content = MagicMock()
    content_map = {
        (r["rule_fqdn"], r["error_key"]): {
            "description": r["description"],
            "total_risk": r["total_risk"],
        }
        for r in test_rules
        if "description" in r
    }
    mock_content.get_content.side_effect = lambda fqdn, ek: content_map.get((fqdn, ek))

    app.state.content_service = mock_content
    try:
        response = client.get(
            f"/api/v2/cluster/{CLUSTER_ID}/request/{REQUEST_ID}/report"
        )
    finally:
        del app.state.content_service

    assert response.status_code == 200
    data = response.json()

    assert data["cluster"] == CLUSTER_ID
    assert data["requestID"] == REQUEST_ID
    assert data["status"] == "processed"

    # Check individual rule reports
    # Rules without content are skipped (matches smart-proxy behavior)
    expected_reports = [r for r in test_rules if "description" in r]
    assert len(data["report"]) == len(expected_reports)
    report_by_ek = {r["error_key"]: r for r in data["report"]}
    for expected_report in expected_reports:
        assert expected_report["error_key"] in report_by_ek
        actual_report = report_by_ek[expected_report["error_key"]]
        assert len(actual_report.items()) == 4
        for key, value in expected_report.items():
            assert actual_report[key] == value


@pytest.mark.parametrize(
    "endpoint",
    [
        f"/api/v2/cluster/{CLUSTER_ID}/request/nonexistent/report",
        f"/api/v2/cluster/{CLUSTER_ID}/request/nonexistent/status",
        f"/api/v2/cluster/nonexistent/request/{REQUEST_ID}/report"
        f"/api/v2/cluster/nonexistent/request/{REQUEST_ID}/status",
    ],
)
def test_not_found(endpoint, database):
    """Test report/status endpoint returns 404 for unknown cluster ID (or request ID)."""
    RequestReport.create(
        db=database,
        request_id=REQUEST_ID,
        cluster_id=CLUSTER_ID,
        report="[]",
    )
    database.commit()

    response = client.get(endpoint)
    assert response.status_code == 404


def test_cleanup_removes_old_request_reports(database):
    """Test that delete_older_than removes expired reports and leaves recent ones."""
    old_record = RequestReport(
        request_id=OLD_REQUEST_ID,
        cluster_id=CLUSTER_ID,
        report="[]",
        created_at=datetime.now(timezone.utc) - timedelta(hours=48),
    )
    recent_record = RequestReport(
        request_id=REQUEST_ID,
        cluster_id=CLUSTER_ID,
        report="[]",
        created_at=datetime.now(timezone.utc),
    )
    database.add_all([old_record, recent_record])
    database.commit()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    deleted = RequestReport.delete_older_than(database, cutoff)
    database.commit()

    assert deleted == 1

    # Old report is gone — status endpoint returns 404
    response = client.get(
        f"/api/v2/cluster/{CLUSTER_ID}/request/{OLD_REQUEST_ID}/status"
    )
    assert response.status_code == 404

    # Recent report still accessible
    response = client.get(f"/api/v2/cluster/{CLUSTER_ID}/request/{REQUEST_ID}/status")
    assert response.status_code == 200
