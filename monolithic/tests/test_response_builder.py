"""Tests for ResponseBuilder."""
from datetime import datetime
from unittest.mock import Mock

import pytest

from app.schemas import RuleHitDetailedResponse
from app.utils.response_builder import ResponseBuilder


def test_build_rule_hit_v2_basic():
    """Test building a basic rule hit response."""
    # Create mock RuleHit
    hit = Mock()
    hit.rule_fqdn = "ccx_rules_ocp.external.rules.test_rule"
    hit.error_key = "TEST_ERROR"
    hit.updated_at = datetime(2024, 1, 15, 10, 30, 0)

    # Content data
    content_data = {
        "description": "Test description",
        "generic": "Test generic details",
        "reason": "Test reason",
        "resolution": "Test resolution",
        "more_info": "https://example.com",
        "total_risk": 3,
        "tags": ["test", "critical"],
    }

    # Insights details
    insights_details = {
        "info": "Additional insights info",
        "affected_objects": ["obj1", "obj2"],
    }

    # Build response
    response = ResponseBuilder.build_rule_hit_v2(
        hit, content_data, insights_details
    )

    # Verify
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
    hit.updated_at = datetime(2024, 1, 15, 10, 30, 0)

    content_data = {
        "description": "Test",
        "total_risk": 2,
    }

    publish_date = "2024-01-10T08:00:00Z"

    response = ResponseBuilder.build_rule_hit_v2(
        hit, content_data, {}, publish_date
    )

    assert response.created_at == "2024-01-10T08:00:00Z"


def test_build_rule_hit_v2_extra_data():
    """Test that extra_data includes insights details and error_key."""
    hit = Mock()
    hit.rule_fqdn = "test.rule"
    hit.error_key = "ERROR_KEY"
    hit.updated_at = datetime(2024, 1, 15, 10, 30, 0)

    content_data = {"description": "Test"}

    insights_details = {
        "custom_field": "custom_value",
        "count": 42,
    }

    response = ResponseBuilder.build_rule_hit_v2(
        hit, content_data, insights_details
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
    hit.updated_at = datetime(2024, 1, 15, 10, 30, 0)

    # Minimal content data
    content_data = {}

    response = ResponseBuilder.build_rule_hit_v2(hit, content_data, {})

    assert response.description == ""
    assert response.details == ""
    assert response.reason == ""
    assert response.resolution == ""
    assert response.more_info == ""
    assert response.total_risk == 1
    assert response.tags == []


def test_build_rule_hit_v2_invalid_publish_date_fallback():
    """Test that invalid publish date falls back to updated_at."""
    hit = Mock()
    hit.rule_fqdn = "test.rule"
    hit.error_key = "ERROR"
    hit.updated_at = datetime(2024, 1, 15, 10, 30, 0)

    content_data = {"description": "Test"}

    # Invalid publish date
    publish_date = "invalid-date"

    response = ResponseBuilder.build_rule_hit_v2(
        hit, content_data, {}, publish_date
    )

    # Should fall back to updated_at
    assert response.created_at == "2024-01-15T10:30:00Z"


def test_build_rule_hit_v2_impacted_timestamp():
    """Test that impacted timestamp is set from updated_at."""
    hit = Mock()
    hit.rule_fqdn = "test.rule"
    hit.error_key = "ERROR"
    hit.updated_at = datetime(2024, 2, 20, 14, 45, 30)

    content_data = {"description": "Test"}

    response = ResponseBuilder.build_rule_hit_v2(hit, content_data, {})

    assert response.impacted == "2024-02-20T14:45:30Z"
