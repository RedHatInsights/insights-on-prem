"""Tests for YAML content parser."""
import pytest
import yaml
from pathlib import Path

from app.content_parser_yaml import YAMLContentParser
from app.exceptions import ProcessingError


def test_init_with_valid_path(tmp_path):
    """Test initialization with valid content path."""
    content_dir = tmp_path / "content"
    content_dir.mkdir()

    parser = YAMLContentParser(str(content_dir))

    assert parser.content_path == content_dir


def test_init_with_missing_path():
    """Test initialization with missing content path raises error."""
    with pytest.raises(ProcessingError, match="Rules content directory not found"):
        YAMLContentParser("/nonexistent/path")


def test_init_with_default_path():
    """Test initialization with default path."""
    # This will fail because default path won't exist in tests
    with pytest.raises(ProcessingError):
        YAMLContentParser()


def test_load_impact_mapping_success(tmp_path):
    """Test loading impact mapping from config.yaml."""
    content_dir = tmp_path / "content"
    content_dir.mkdir()

    # Create config.yaml with impact mapping
    config_data = {
        "impact": {
            "critical": 4,
            "high": 3,
            "medium": 2,
            "low": 1
        }
    }
    config_file = content_dir / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)

    parser = YAMLContentParser(str(content_dir))

    assert parser.impact_mapping == config_data["impact"]
    assert parser.impact_mapping["critical"] == 4
    assert parser.impact_mapping["low"] == 1


def test_load_impact_mapping_missing_config(tmp_path):
    """Test loading impact mapping when config.yaml is missing."""
    content_dir = tmp_path / "content"
    content_dir.mkdir()

    parser = YAMLContentParser(str(content_dir))

    assert parser.impact_mapping == {}


def test_load_impact_mapping_invalid_yaml(tmp_path):
    """Test loading impact mapping from invalid YAML."""
    content_dir = tmp_path / "content"
    content_dir.mkdir()

    # Create invalid YAML file
    config_file = content_dir / "config.yaml"
    config_file.write_text("invalid: yaml: content:")

    parser = YAMLContentParser(str(content_dir))

    assert parser.impact_mapping == {}


def test_parse_all_rules_empty_directory(tmp_path):
    """Test parsing when no rules exist."""
    content_dir = tmp_path / "content"
    content_dir.mkdir()

    parser = YAMLContentParser(str(content_dir))
    rules = parser.parse_all_rules()

    assert rules == []


def _create_rule_with_plugin(rule_dir, error_key, metadata):
    """Helper to create a rule directory with plugin.yaml and error key."""
    rule_dir.mkdir(parents=True, exist_ok=True)

    # Create plugin.yaml (required by parser)
    plugin_data = {"plugin": {"name": rule_dir.name}}
    with open(rule_dir / "plugin.yaml", "w") as f:
        yaml.dump(plugin_data, f)

    # Create error key directory
    error_key_dir = rule_dir / error_key
    error_key_dir.mkdir()

    # Create metadata.yaml
    metadata_file = error_key_dir / "metadata.yaml"
    with open(metadata_file, "w") as f:
        yaml.dump(metadata, f)

    return error_key_dir


def test_parse_all_rules_with_external_rules(tmp_path):
    """Test parsing external rules."""
    content_dir = tmp_path / "content"
    content_dir.mkdir()

    # Create external/rules directory structure
    external_dir = content_dir / "external" / "rules"

    rule_dir = external_dir / "example_rule"

    metadata = {
        "impact": {"name": "medium", "impact": 2},
        "likelihood": 3,
        "resolution": "Fix the issue",
        "reason": "Because of reasons",
        "more_info": "https://example.com",
        "tags": ["test", "example"],
        "publish_date": "2024-01-01"
    }

    error_key_dir = _create_rule_with_plugin(rule_dir, "ERROR_KEY", metadata)

    # Create generic.md for description
    (error_key_dir / "generic.md").write_text("Test rule description")

    parser = YAMLContentParser(str(content_dir))
    rules = parser.parse_all_rules()

    assert len(rules) == 1
    # Code builds: ccx_rules_ocp.{rule_type}.{rule_name} (no .report suffix)
    assert rules[0]["rule_fqdn"] == "ccx_rules_ocp.external.rules.example_rule"
    assert rules[0]["error_key"] == "ERROR_KEY"
    assert rules[0]["description"] == "Test rule description"
    assert rules[0]["impact"] == 2
    assert rules[0]["likelihood"] == 3
    assert rules[0]["resolution"] == "Fix the issue"


def test_parse_all_rules_with_internal_rules(tmp_path):
    """Test parsing internal rules."""
    content_dir = tmp_path / "content"
    content_dir.mkdir()

    # Create internal/rules directory structure
    internal_dir = content_dir / "internal" / "rules"
    rule_dir = internal_dir / "internal_rule"

    metadata = {
        "impact": {"name": "high", "impact": 3}
    }

    _create_rule_with_plugin(rule_dir, "INTERNAL_ERROR", metadata)

    parser = YAMLContentParser(str(content_dir))
    rules = parser.parse_all_rules()

    assert len(rules) == 1
    assert rules[0]["rule_fqdn"] == "ccx_rules_ocp.internal.rules.internal_rule"
    assert rules[0]["error_key"] == "INTERNAL_ERROR"


def test_parse_all_rules_with_ocs_rules(tmp_path):
    """Test parsing OCS rules from external/ocs directory."""
    content_dir = tmp_path / "content"
    content_dir.mkdir()

    # Create external/ocs directory structure
    ocs_dir = content_dir / "external" / "ocs"
    rule_dir = ocs_dir / "ceph_rule"

    metadata = {
        "impact": {"name": "critical", "impact": 4}
    }

    _create_rule_with_plugin(rule_dir, "CEPH_ERROR", metadata)

    parser = YAMLContentParser(str(content_dir))
    rules = parser.parse_all_rules()

    assert len(rules) == 1
    assert rules[0]["rule_fqdn"] == "ccx_rules_ocp.external.ocs.ceph_rule"
    assert rules[0]["error_key"] == "CEPH_ERROR"


def test_parse_rule_directory_with_multiple_error_keys(tmp_path):
    """Test parsing rule with multiple error keys."""
    content_dir = tmp_path / "content"
    content_dir.mkdir()

    external_dir = content_dir / "external" / "rules"
    rule_dir = external_dir / "multi_error_rule"
    rule_dir.mkdir(parents=True)

    # Create plugin.yaml
    plugin_data = {"plugin": {"name": "multi_error_rule"}}
    with open(rule_dir / "plugin.yaml", "w") as f:
        yaml.dump(plugin_data, f)

    # Create first error key
    error_key1_dir = rule_dir / "ERROR_1"
    error_key1_dir.mkdir()
    metadata1 = {"impact": {"name": "low", "impact": 1}}
    with open(error_key1_dir / "metadata.yaml", "w") as f:
        yaml.dump(metadata1, f)

    # Create second error key
    error_key2_dir = rule_dir / "ERROR_2"
    error_key2_dir.mkdir()
    metadata2 = {"impact": {"name": "high", "impact": 3}}
    with open(error_key2_dir / "metadata.yaml", "w") as f:
        yaml.dump(metadata2, f)

    parser = YAMLContentParser(str(content_dir))
    rules = parser.parse_all_rules()

    assert len(rules) == 2
    error_keys = {r["error_key"] for r in rules}
    assert error_keys == {"ERROR_1", "ERROR_2"}


def test_parse_error_key_directory_with_missing_metadata(tmp_path):
    """Test parsing error key directory without metadata.yaml."""
    content_dir = tmp_path / "content"
    content_dir.mkdir()

    external_dir = content_dir / "external" / "rules"
    rule_dir = external_dir / "rule_no_metadata"
    rule_dir.mkdir(parents=True)

    # Create plugin.yaml
    plugin_data = {"plugin": {"name": "rule_no_metadata"}}
    with open(rule_dir / "plugin.yaml", "w") as f:
        yaml.dump(plugin_data, f)

    error_key_dir = rule_dir / "ERROR_KEY"
    error_key_dir.mkdir()
    # No metadata.yaml file

    parser = YAMLContentParser(str(content_dir))
    rules = parser.parse_all_rules()

    # Should still parse (with defaults), since plugin.yaml exists
    assert len(rules) == 1


def test_parse_error_key_directory_with_invalid_metadata(tmp_path):
    """Test parsing error key with invalid metadata.yaml."""
    content_dir = tmp_path / "content"
    content_dir.mkdir()

    external_dir = content_dir / "external" / "rules"
    rule_dir = external_dir / "rule_bad_metadata"
    rule_dir.mkdir(parents=True)

    # Create plugin.yaml
    plugin_data = {"plugin": {"name": "rule_bad_metadata"}}
    with open(rule_dir / "plugin.yaml", "w") as f:
        yaml.dump(plugin_data, f)

    error_key_dir = rule_dir / "ERROR_KEY"
    error_key_dir.mkdir()

    # Create invalid YAML
    metadata_file = error_key_dir / "metadata.yaml"
    metadata_file.write_text("invalid: yaml: content:")

    parser = YAMLContentParser(str(content_dir))
    rules = parser.parse_all_rules()

    # Should not fail, just skip the error key
    assert len(rules) == 0


def test_parse_all_rules_total_risk_calculation(tmp_path):
    """Test that total_risk is calculated correctly."""
    content_dir = tmp_path / "content"
    content_dir.mkdir()

    external_dir = content_dir / "external" / "rules"
    rule_dir = external_dir / "risk_rule"

    # Create metadata with impact and likelihood
    metadata = {
        "impact": {"name": "high", "impact": 3},
        "likelihood": 2
    }

    _create_rule_with_plugin(rule_dir, "ERROR_KEY", metadata)

    parser = YAMLContentParser(str(content_dir))
    rules = parser.parse_all_rules()

    assert len(rules) == 1
    # total_risk = (impact + likelihood) // 2 = (3 + 2) // 2 = 2 (integer division)
    assert rules[0]["total_risk"] == 2
