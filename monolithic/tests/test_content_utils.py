"""Tests for content utilities."""
from datetime import datetime

import pytest

from app.utils.content_utils import normalize_rule_fqdn, format_datetime_rfc3339


@pytest.mark.parametrize("rule_fqdn, expected", [
    ("ccx_rules_ocp.external.rules.some_rule.report", "ccx_rules_ocp.external.rules.some_rule"),
    ("ccx_rules_ocp.external.rules.some_rule", "ccx_rules_ocp.external.rules.some_rule"),
    ("", ""),
    ("ccx_rules_ocp.external.report.some_rule.report", "ccx_rules_ocp.external.report.some_rule"),
])
def test_normalize_rule_fqdn(rule_fqdn, expected):
    """Test normalization of rule FQDN."""
    assert normalize_rule_fqdn(rule_fqdn) == expected


@pytest.mark.parametrize("dt, expected", [
    (datetime(2024, 1, 15, 10, 30, 45), "2024-01-15T10:30:45Z"),
    (None, None),
    (datetime(2024, 1, 5, 8, 3, 2), "2024-01-05T08:03:02Z"),
    (datetime(2024, 12, 31, 0, 0, 0), "2024-12-31T00:00:00Z"),
])
def test_format_datetime_rfc3339(dt, expected):
    """Test RFC3339 datetime formatting."""
    assert format_datetime_rfc3339(dt) == expected
