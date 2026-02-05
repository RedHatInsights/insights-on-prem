"""Shared utilities for content handling."""
from datetime import datetime
from typing import Optional


def normalize_rule_fqdn(rule_fqdn: str) -> str:
    """
    Strip .report suffix from rule FQDN for content lookup.

    :param rule_fqdn: Fully qualified rule name
    :return: Normalized rule FQDN without .report suffix
    """
    if rule_fqdn.endswith(".report"):
        return rule_fqdn.replace(".report", "")
    return rule_fqdn


def format_datetime_rfc3339(dt: Optional[datetime]) -> Optional[str]:
    """
    Format datetime as RFC3339 string.

    :param dt: Datetime object
    :return: RFC3339 formatted string or None
    """
    if dt:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return None
