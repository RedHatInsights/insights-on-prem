"""Configuration loader."""

import logging
import logging.config
import os

import yaml
from insights import apply_configs, apply_default_enabled, dr

from app.config import AppConfig, apply_env_overrides
from app.exceptions import ProcessingError

logger = logging.getLogger(__name__)


def load_config(config_path: str = "config.yml") -> AppConfig:
    """Load configuration from YAML file with environment variable overrides.

    :param config_path: Path to the YAML configuration file
    :return: AppConfig instance
    :raises ProcessingError: If config file cannot be loaded
    """
    if not os.path.exists(config_path):
        logger.error(f"Config file {config_path} not found")
        raise ProcessingError(f"Configuration file not found: {config_path}")

    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        raise ProcessingError(f"Configuration loading failed: {str(e)}") from e

    plugins = raw.get("plugins", {})
    config = AppConfig(
        **raw.get("service", {}),
        plugin_packages=plugins.get("packages", []),
        plugin_configs=plugins.get("configs", []),
    )

    apply_env_overrides(config)

    # Apply logging configuration
    logging_config = raw.get("logging")
    if logging_config:
        logging.config.dictConfig(logging_config)

    logger.info(f"Loaded configuration from {config_path}")
    return config


def load_insights_components(config: AppConfig) -> None:
    """Load insights-core components based on configuration.

    :param config: AppConfig instance
    """
    for package in config.plugin_packages:
        logger.info(f"Loading package: {package}")
        try:
            dr.load_components(package, continue_on_error=False)
        except Exception as e:
            logger.error(f"Failed to load package {package}: {e}")
            raise ProcessingError(
                f"Failed to load required package '{package}': {str(e)}"
            ) from e

    plugins = {"packages": config.plugin_packages, "configs": config.plugin_configs}
    apply_default_enabled(plugins)
    apply_configs(plugins)

    logger.info(f"Successfully loaded packages: {', '.join(config.plugin_packages)}")
    logger.info("Insights-core components loading completed")
