"""Tests for content utilities."""
from datetime import datetime

import pytest

from app.utils.content_utils import normalize_rule_fqdn, format_datetime_rfc3339


def test_normalize_rule_fqdn_removes_report_suffix():
    """Test that .report suffix is removed."""
    rule_fqdn = "ccx_rules_ocp.external.rules.some_rule.report"
    expected = "ccx_rules_ocp.external.rules.some_rule"
    assert normalize_rule_fqdn(rule_fqdn) == expected


def test_normalize_rule_fqdn_no_suffix_unchanged():
    """Test that rule without .report suffix is unchanged."""
    rule_fqdn = "ccx_rules_ocp.external.rules.some_rule"
    assert normalize_rule_fqdn(rule_fqdn) == rule_fqdn


def test_normalize_rule_fqdn_empty_string():
    """Test empty string input."""
    assert normalize_rule_fqdn("") == ""


def test_normalize_rule_fqdn_multiple_report_occurrences():
    """Test that only .report suffix is removed, not all occurrences."""
    rule_fqdn = "ccx_rules_ocp.external.report.some_rule.report"
    expected = "ccx_rules_ocp.external.report.some_rule"
    assert normalize_rule_fqdn(rule_fqdn) == expected


def test_format_datetime_rfc3339_formats_correctly():
    """Test that datetime is formatted as RFC3339."""
    dt = datetime(2024, 1, 15, 10, 30, 45)
    expected = "2024-01-15T10:30:45Z"
    assert format_datetime_rfc3339(dt) == expected


def test_format_datetime_rfc3339_none_returns_none():
    """Test that None input returns None."""
    assert format_datetime_rfc3339(None) is None


def test_format_datetime_rfc3339_zero_padded_values():
    """Test that single-digit values are zero-padded."""
    dt = datetime(2024, 1, 5, 8, 3, 2)
    expected = "2024-01-05T08:03:02Z"
    assert format_datetime_rfc3339(dt) == expected


def test_format_datetime_rfc3339_midnight_time():
    """Test formatting datetime at midnight."""
    dt = datetime(2024, 12, 31, 0, 0, 0)
    expected = "2024-12-31T00:00:00Z"
    assert format_datetime_rfc3339(dt) == expected
