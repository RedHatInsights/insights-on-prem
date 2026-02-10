"""Tests for config loader."""
import pytest
import yaml
from unittest.mock import patch, Mock

from app.services.config_loader import load_insights_config, load_insights_components
from app.exceptions import ProcessingError


def test_load_insights_config_success(tmp_path):
    """Test loading valid config file."""
    config_file = tmp_path / "config.yml"
    config_data = {
        "plugins": {
            "packages": ["package1", "package2"],
            "configs": []
        },
        "service": {
            "extract_timeout": 600,
            "extract_tmp_dir": "/tmp/custom",
        }
    }

    with open(config_file, "w") as f:
        yaml.dump(config_data, f)

    config = load_insights_config(str(config_file))

    assert config["plugins"]["packages"] == ["package1", "package2"]
    assert config["service"]["extract_timeout"] == 600


def test_load_insights_config_file_not_found():
    """Test loading config when file doesn't exist."""
    with pytest.raises(ProcessingError, match="Configuration file not found"):
        load_insights_config("/nonexistent/config.yml")


def test_load_insights_config_invalid_yaml(tmp_path):
    """Test loading config with invalid YAML."""
    config_file = tmp_path / "config.yml"
    config_file.write_text("invalid: yaml: content:")

    with pytest.raises(ProcessingError, match="Configuration loading failed"):
        load_insights_config(str(config_file))


def test_load_insights_config_empty_file(tmp_path):
    """Test loading empty config file returns None."""
    config_file = tmp_path / "config.yml"
    config_file.write_text("")

    result = load_insights_config(str(config_file))
    assert result is None


@patch('app.services.config_loader.apply_configs')
@patch('app.services.config_loader.apply_default_enabled')
@patch('app.services.config_loader.dr')
def test_load_insights_components_success(mock_dr, mock_apply_default, mock_apply_configs):
    """Test loading components successfully."""
    config = {
        "plugins": {
            "packages": ["package1", "package2"],
            "configs": []
        }
    }

    load_insights_components(config)

    # Verify packages were loaded
    assert mock_dr.load_components.call_count == 2
    mock_dr.load_components.assert_any_call("package1", continue_on_error=False)
    mock_dr.load_components.assert_any_call("package2", continue_on_error=False)

    # Verify apply functions were called
    mock_apply_default.assert_called_once_with(config["plugins"])
    mock_apply_configs.assert_called_once_with(config["plugins"])


@patch('app.services.config_loader.apply_configs')
@patch('app.services.config_loader.apply_default_enabled')
@patch('app.services.config_loader.dr')
def test_load_insights_components_package_load_fails(mock_dr, mock_apply_default, mock_apply_configs):
    """Test component loading when package fails to load."""
    config = {
        "plugins": {
            "packages": ["failing_package"],
            "configs": []
        }
    }

    mock_dr.load_components.side_effect = ImportError("Package not found")

    with pytest.raises(ProcessingError, match="Failed to load required package"):
        load_insights_components(config)


@patch('app.services.config_loader.apply_configs')
@patch('app.services.config_loader.apply_default_enabled')
@patch('app.services.config_loader.dr')
def test_load_insights_components_empty_packages(mock_dr, mock_apply_default, mock_apply_configs):
    """Test loading components with no packages."""
    config = {
        "plugins": {
            "packages": [],
            "configs": []
        }
    }

    load_insights_components(config)

    # Should not try to load any packages
    mock_dr.load_components.assert_not_called()


@patch('app.services.config_loader.apply_configs')
@patch('app.services.config_loader.apply_default_enabled')
@patch('app.services.config_loader.dr')
def test_load_insights_components_no_plugins_key(mock_dr, mock_apply_default, mock_apply_configs):
    """Test loading components when config has no plugins key."""
    config = {}

    load_insights_components(config)

    # Should handle missing plugins gracefully
    mock_dr.load_components.assert_not_called()


@patch('app.services.config_loader.apply_configs')
@patch('app.services.config_loader.apply_default_enabled')
@patch('app.services.config_loader.dr')
def test_load_insights_components_multiple_packages(mock_dr, mock_apply_default, mock_apply_configs):
    """Test loading multiple packages."""
    config = {
        "plugins": {
            "packages": [
                "ccx_rules_ocp.external",
                "ccx_rules_processing",
                "custom_package"
            ],
            "configs": []
        }
    }

    load_insights_components(config)

    assert mock_dr.load_components.call_count == 3
    mock_dr.load_components.assert_any_call("ccx_rules_ocp.external", continue_on_error=False)
    mock_dr.load_components.assert_any_call("ccx_rules_processing", continue_on_error=False)
    mock_dr.load_components.assert_any_call("custom_package", continue_on_error=False)


@patch('app.services.config_loader.apply_configs')
@patch('app.services.config_loader.apply_default_enabled')
@patch('app.services.config_loader.dr')
def test_load_insights_components_partial_failure(mock_dr, mock_apply_default, mock_apply_configs):
    """Test that any package failure stops the process."""
    config = {
        "plugins": {
            "packages": ["good_package", "bad_package", "another_good_package"],
            "configs": []
        }
    }

    # First package succeeds, second fails
    mock_dr.load_components.side_effect = [None, Exception("Load failed"), None]

    with pytest.raises(ProcessingError, match="Failed to load required package 'bad_package'"):
        load_insights_components(config)

    # Should have tried to load the first two packages
    assert mock_dr.load_components.call_count == 2
