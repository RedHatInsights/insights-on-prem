"""Tests for config loader."""
import pytest
import yaml
from unittest.mock import patch

from app.config import AppConfig
from app.config_loader import load_config, load_insights_components
from app.exceptions import ProcessingError


def test_load_config_success(tmp_path):
    """Test loading valid config file."""
    config_file = tmp_path / "config.yml"
    config_data = {
        "service": {
            "postgres_host": "dbhost",
            "postgres_port": 5433,
            "max_file_size": 50000,
            "extract_timeout": 600,
            "temp_upload_dir": "/tmp/custom",
        },
        "plugins": {
            "packages": ["package1", "package2"],
            "configs": [],
        },
    }

    with open(config_file, "w") as f:
        yaml.dump(config_data, f)

    config = load_config(str(config_file))

    assert isinstance(config, AppConfig)
    assert config.postgres_host == "dbhost"
    assert config.postgres_port == 5433
    assert config.max_file_size == 50000
    assert config.plugin_packages == ["package1", "package2"]
    assert config.extract_timeout == 600
    assert config.temp_upload_dir == "/tmp/custom"


def test_load_config_env_overrides(tmp_path, monkeypatch):
    """Test that environment variables override YAML values."""
    config_file = tmp_path / "config.yml"
    config_data = {
        "service": {
            "postgres_host": "yaml-host",
            "postgres_port": 5432,
        },
    }

    with open(config_file, "w") as f:
        yaml.dump(config_data, f)

    monkeypatch.setenv("POSTGRES_HOST", "env-host")
    monkeypatch.setenv("POSTGRES_PORT", "5433")

    config = load_config(str(config_file))

    assert config.postgres_host == "env-host"
    assert config.postgres_port == 5433


def test_load_config_file_not_found():
    """Test loading config when file doesn't exist."""
    with pytest.raises(ProcessingError, match="Configuration file not found"):
        load_config("/nonexistent/config.yml")


def test_load_config_invalid_yaml(tmp_path):
    """Test loading config with invalid YAML."""
    config_file = tmp_path / "config.yml"
    config_file.write_text("invalid: yaml: content:")

    with pytest.raises(ProcessingError, match="Configuration loading failed"):
        load_config(str(config_file))


def test_load_config_empty_file(tmp_path):
    """Test loading empty config file returns AppConfig with defaults."""
    config_file = tmp_path / "config.yml"
    config_file.write_text("")

    config = load_config(str(config_file))

    assert isinstance(config, AppConfig)
    assert config.postgres_host == "localhost"
    assert config.extract_timeout == 300


@patch('app.config_loader.apply_configs')
@patch('app.config_loader.apply_default_enabled')
@patch('app.config_loader.dr')
def test_load_insights_components_success(mock_dr, mock_apply_default, mock_apply_configs):
    """Test loading components successfully."""
    config = AppConfig(
        plugin_packages=["package1", "package2"],
        plugin_configs=[],
    )

    load_insights_components(config)

    assert mock_dr.load_components.call_count == 2
    mock_dr.load_components.assert_any_call("package1", continue_on_error=False)
    mock_dr.load_components.assert_any_call("package2", continue_on_error=False)

    expected_plugins = {"packages": ["package1", "package2"], "configs": []}
    mock_apply_default.assert_called_once_with(expected_plugins)
    mock_apply_configs.assert_called_once_with(expected_plugins)


@patch('app.config_loader.apply_configs')
@patch('app.config_loader.apply_default_enabled')
@patch('app.config_loader.dr')
def test_load_insights_components_package_load_fails(mock_dr, mock_apply_default, mock_apply_configs):
    """Test component loading when package fails to load."""
    config = AppConfig(plugin_packages=["failing_package"])

    mock_dr.load_components.side_effect = ImportError("Package not found")

    with pytest.raises(ProcessingError, match="Failed to load required package"):
        load_insights_components(config)


@patch('app.config_loader.apply_configs')
@patch('app.config_loader.apply_default_enabled')
@patch('app.config_loader.dr')
def test_load_insights_components_empty_packages(mock_dr, mock_apply_default, mock_apply_configs):
    """Test loading components with no packages."""
    config = AppConfig(plugin_packages=[])

    load_insights_components(config)

    mock_dr.load_components.assert_not_called()


@patch('app.config_loader.apply_configs')
@patch('app.config_loader.apply_default_enabled')
@patch('app.config_loader.dr')
def test_load_insights_components_no_plugins(mock_dr, mock_apply_default, mock_apply_configs):
    """Test loading components when no plugins configured."""
    config = AppConfig()

    load_insights_components(config)

    mock_dr.load_components.assert_not_called()


@patch('app.config_loader.apply_configs')
@patch('app.config_loader.apply_default_enabled')
@patch('app.config_loader.dr')
def test_load_insights_components_multiple_packages(mock_dr, mock_apply_default, mock_apply_configs):
    """Test loading multiple packages."""
    config = AppConfig(
        plugin_packages=["ccx_rules_ocp.external", "ccx_rules_processing", "custom_package"],
    )

    load_insights_components(config)

    assert mock_dr.load_components.call_count == 3
    mock_dr.load_components.assert_any_call("ccx_rules_ocp.external", continue_on_error=False)
    mock_dr.load_components.assert_any_call("ccx_rules_processing", continue_on_error=False)
    mock_dr.load_components.assert_any_call("custom_package", continue_on_error=False)


@patch('app.config_loader.apply_configs')
@patch('app.config_loader.apply_default_enabled')
@patch('app.config_loader.dr')
def test_load_insights_components_partial_failure(mock_dr, mock_apply_default, mock_apply_configs):
    """Test that any package failure stops the process."""
    config = AppConfig(
        plugin_packages=["good_package", "bad_package", "another_good_package"],
    )

    mock_dr.load_components.side_effect = [None, Exception("Load failed"), None]

    with pytest.raises(ProcessingError, match="Failed to load required package 'bad_package'"):
        load_insights_components(config)

    assert mock_dr.load_components.call_count == 2
