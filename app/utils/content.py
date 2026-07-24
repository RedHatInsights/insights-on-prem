"""Shared utilities for content handling."""

from datetime import datetime


def normalize_rule_fqdn(rule_fqdn: str) -> str:
    """
    Strip the function name suffix from rule FQDN for content lookup.

    insights-core's dr.get_name() returns "module.function_name" (e.g.
    "ccx_rules_ocp.external.rules.some_rule.report"), but the content
    dictionary is keyed by module name only. This strips the last component.

    :param rule_fqdn: Fully qualified rule name from insights-core
    :return: Module path without the function name suffix
    """
    return rule_fqdn.rsplit(".", 1)[0] if "." in rule_fqdn else rule_fqdn


def format_datetime_rfc3339(dt: datetime | None) -> str | None:
    """
    Format datetime as RFC3339 string.

    :param dt: Datetime object
    :return: RFC3339 formatted string or None
    """
    if dt:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return None
