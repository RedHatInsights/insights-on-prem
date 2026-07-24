"""Tests for YAML content parser."""

import os

import pytest
from app.content_parser_yaml import YAMLContentParser
from app.exceptions import ProcessingError

CONTENT_DIR = os.path.join(os.path.dirname(__file__), "content")


def test_init_with_valid_path():
    """Test initialization with valid content path."""
    parser = YAMLContentParser(os.path.join(CONTENT_DIR, "ok"))

    assert parser.content_path.name == "ok"


def test_init_with_missing_path():
    """Test initialization with missing content path raises error."""
    with pytest.raises(ProcessingError, match="Rules content directory not found"):
        YAMLContentParser("/nonexistent/path")


def test_load_impact_mapping_success():
    """Test loading impact mapping from config.yaml."""
    parser = YAMLContentParser(os.path.join(CONTENT_DIR, "ok"))

    assert parser.impact_mapping["One"] == 1
    assert parser.impact_mapping["Two"] == 2
    assert parser.impact_mapping["Four"] == 4


def test_load_impact_mapping_missing_config():
    """Test loading impact mapping when config.yaml is missing."""
    parser = YAMLContentParser(os.path.join(CONTENT_DIR, "missing_config"))

    assert parser.impact_mapping == {}


def test_load_impact_mapping_invalid_yaml():
    """Test loading impact mapping from invalid YAML."""
    parser = YAMLContentParser(os.path.join(CONTENT_DIR, "bad_config"))

    assert parser.impact_mapping == {}


def test_parse_all_rules_empty_directory():
    """Test parsing when no rules exist."""
    parser = YAMLContentParser(os.path.join(CONTENT_DIR, "ok_no_content"))
    rules = parser.parse_all_rules()

    assert rules == []


def test_parse_all_rules_with_external_rules():
    """Test parsing external rules."""
    parser = YAMLContentParser(os.path.join(CONTENT_DIR, "ok"))
    rules = parser.parse_all_rules()

    rule1 = next(
        r for r in rules if r["rule_fqdn"] == "ccx_rules_ocp.external.rules.rule1"
    )

    assert rule1["error_key"] == "err_key"
    assert rule1["description"] == "Generic message for rule1"
    # impact: "Two" -> resolved to 2 via config.yaml mapping
    assert rule1["impact"] == 2
    assert rule1["likelihood"] == 2
    assert rule1["tags"] == ["security", "incident"]
    assert rule1["publish_date"] == "2020-04-03T16:13:30+02:00"


def test_parse_all_rules_count():
    """Test that all rules across external and OCS are parsed."""
    parser = YAMLContentParser(os.path.join(CONTENT_DIR, "ok"))
    rules = parser.parse_all_rules()

    assert len(rules) == 2
    fqdns = {r["rule_fqdn"] for r in rules}
    assert fqdns == {
        "ccx_rules_ocp.external.rules.rule1",
        "ccx_rules_ocp.external.rules.rule2",
    }


def test_parse_all_rules_markdown_files():
    """Test that markdown files are read and stripped correctly."""
    parser = YAMLContentParser(os.path.join(CONTENT_DIR, "ok"))
    rules = parser.parse_all_rules()

    rule1 = next(
        r for r in rules if r["rule_fqdn"] == "ccx_rules_ocp.external.rules.rule1"
    )

    assert rule1["generic"] == "Generic message for rule1"
    assert "# Some more information" in rule1["more_info"]
    assert "### into this file" in rule1["more_info"]


def test_parse_all_rules_only_ek_level():
    """Test parsing content where all files are at error key level."""
    parser = YAMLContentParser(os.path.join(CONTENT_DIR, "ok_only_ek_level"))
    rules = parser.parse_all_rules()

    assert len(rules) == 1
    rule = rules[0]
    assert rule["rule_fqdn"] == "ccx_rules_ocp.external.rules.rule1"
    assert rule["description"] == "Generic message at error key level"
    assert rule["reason"] == "Reason at error key level"
    assert rule["resolution"] == "Resolution at error key level"
    assert rule["more_info"] == "More info at error key level"


def test_parse_error_key_directory_with_missing_metadata():
    """Test parsing error key directory without metadata.yaml."""
    parser = YAMLContentParser(os.path.join(CONTENT_DIR, "missing_metadata"))
    rules = parser.parse_all_rules()

    assert len(rules) == 1


def test_parse_error_key_directory_with_invalid_metadata():
    """Test parsing error key with invalid metadata.yaml."""
    parser = YAMLContentParser(os.path.join(CONTENT_DIR, "bad_metadata"))
    rules = parser.parse_all_rules()

    assert len(rules) == 0


def test_parse_all_rules_total_risk_calculation():
    """Test that total_risk is calculated correctly."""
    parser = YAMLContentParser(os.path.join(CONTENT_DIR, "ok"))
    rules = parser.parse_all_rules()

    # rule1: impact=2, likelihood=2 -> total_risk = (2+2)//2 = 2
    rule1 = next(
        r for r in rules if r["rule_fqdn"] == "ccx_rules_ocp.external.rules.rule1"
    )
    assert rule1["total_risk"] == 2

    # rule2: impact=4, likelihood=3 -> total_risk = (4+3)//2 = 3
    rule2 = next(
        r for r in rules if r["rule_fqdn"] == "ccx_rules_ocp.external.rules.rule2"
    )
    assert rule2["total_risk"] == 3
