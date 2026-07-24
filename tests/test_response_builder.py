"""Tests for ResponseBuilder."""

from datetime import datetime
from unittest.mock import Mock

from app.schemas import RuleHitDetailedResponse
from app.utils.response_builder import ResponseBuilder


def test_build_rule_hit_v2_basic():
    """Test building a basic rule hit response."""
    hit = Mock()
    hit.rule_fqdn = "ccx_rules_ocp.external.rules.test_rule"
    hit.error_key = "TEST_ERROR"
    hit.impacted_since = datetime(2024, 1, 10, 8, 0, 0)

    content_data = {
        "description": "Test description",
        "generic": "Test generic details",
        "reason": "Test reason",
        "resolution": "Test resolution",
        "more_info": "https://example.com",
        "total_risk": 3,
        "tags": ["test", "critical"],
    }

    insights_details = {
        "info": "Additional insights info",
        "affected_objects": ["obj1", "obj2"],
    }

    response = ResponseBuilder.build_rule_hit_v2(hit, content_data, insights_details)

    assert isinstance(response, RuleHitDetailedResponse)
    assert response.rule_id == "ccx_rules_ocp.external.rules.test_rule"
    assert response.description == "Test description"
    assert response.details == "Test generic details"
    assert response.reason == "Test reason"
    assert response.resolution == "Test resolution"
    assert response.more_info == "https://example.com"
    assert response.total_risk == 3
    assert response.tags == ["test", "critical"]
    assert response.disabled is False
    assert response.internal is False
    assert response.user_vote == 0


def test_build_rule_hit_v2_with_publish_date():
    """Test building rule hit response with publish date."""
    hit = Mock()
    hit.rule_fqdn = "test.rule"
    hit.error_key = "ERROR"
    hit.impacted_since = datetime(2024, 1, 15, 10, 30, 0)

    content_data = {
        "description": "Test",
        "total_risk": 2,
    }

    response = ResponseBuilder.build_rule_hit_v2(
        hit, content_data, {}, "2024-01-10T08:00:00Z"
    )

    assert response.created_at == "2024-01-10T08:00:00Z"


def test_build_rule_hit_v2_extra_data():
    """Test that extra_data includes insights details and error_key."""
    hit = Mock()
    hit.rule_fqdn = "test.rule"
    hit.error_key = "ERROR_KEY"
    hit.impacted_since = datetime(2024, 1, 15, 10, 30, 0)

    insights_details = {
        "custom_field": "custom_value",
        "count": 42,
    }

    response = ResponseBuilder.build_rule_hit_v2(
        hit, {"description": "Test"}, insights_details
    )

    assert response.extra_data["error_key"] == "ERROR_KEY"
    assert response.extra_data["type"] == "rule"
    assert response.extra_data["custom_field"] == "custom_value"
    assert response.extra_data["count"] == 42


def test_build_rule_hit_v2_missing_optional_fields():
    """Test building response with minimal content data."""
    hit = Mock()
    hit.rule_fqdn = "test.rule"
    hit.error_key = "ERROR"
    hit.impacted_since = datetime(2024, 1, 15, 10, 30, 0)

    response = ResponseBuilder.build_rule_hit_v2(hit, {}, {})

    assert response.description == ""
    assert response.details == ""
    assert response.reason == ""
    assert response.resolution == ""
    assert response.more_info == ""
    assert response.total_risk == 1
    assert response.tags == []


def test_build_rule_hit_v2_invalid_publish_date():
    """Test that invalid publish date results in None created_at."""
    hit = Mock()
    hit.rule_fqdn = "test.rule"
    hit.error_key = "ERROR"
    hit.impacted_since = datetime(2024, 1, 15, 10, 30, 0)

    response = ResponseBuilder.build_rule_hit_v2(
        hit, {"description": "Test"}, {}, "invalid-date"
    )

    assert response.created_at is None


def test_build_rule_hit_v2_impacted_from_impacted_since():
    """Test that impacted timestamp comes from hit.impacted_since."""
    hit = Mock()
    hit.rule_fqdn = "test.rule"
    hit.error_key = "ERROR"
    hit.impacted_since = datetime(2024, 2, 20, 14, 45, 30)

    response = ResponseBuilder.build_rule_hit_v2(hit, {"description": "Test"}, {})

    assert response.impacted == "2024-02-20T14:45:30Z"
