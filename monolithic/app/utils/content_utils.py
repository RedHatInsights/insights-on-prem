"""Shared utilities for content handling."""
from datetime import datetime
from typing import Optional


def normalize_rule_fqdn(rule_fqdn: str) -> str:
    """
    Strip .report suffix from rule FQDN for content lookup.

    Args:
        rule_fqdn: Fully qualified rule name

    Returns:
        Normalized rule FQDN without .report suffix
    """
    if rule_fqdn.endswith(".report"):
        return rule_fqdn.replace(".report", "")
    return rule_fqdn


def format_datetime_rfc3339(dt: Optional[datetime]) -> Optional[str]:
    """
    Format datetime as RFC3339 string.

    Args:
        dt: Datetime object

    Returns:
        RFC3339 formatted string or None
    """
    if dt:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return None
