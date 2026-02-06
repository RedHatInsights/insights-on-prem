"""Configuration loader for insights-core."""
import logging
import os
from typing import Dict
import yaml

from insights import apply_configs, apply_default_enabled, dr

from app.config import get_settings
from app.exceptions import ProcessingError

logger = logging.getLogger(__name__)
settings = get_settings()


def load_insights_config(config_path: str = "config.yml") -> Dict:
    """
    Load insights-core configuration from YAML file.

    :param config_path: Path to the YAML configuration file
    :return: Configuration dictionary
    :raises ProcessingError: If config file cannot be loaded
    """
    if not os.path.exists(config_path):
        logger.error(f"Config file {config_path} not found")
        raise ProcessingError(f"Configuration file not found: {config_path}")

    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)

        logger.info(f"Loaded insights-core configuration from {config_path}")
        return config

    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        raise ProcessingError(f"Configuration loading failed: {str(e)}")


def load_insights_components(config: Dict) -> None:
    """
    Load insights-core components based on configuration.

    :param config: Configuration dictionary from YAML file
    """
    plugins = config.get("plugins", {})
    packages = plugins.get("packages", [])

    # Load each package using dr.load_components from insights-core module
    for package in packages:
        logger.info(f"Loading package: {package}")
        try:
            dr.load_components(package, continue_on_error=False)
        except Exception as e:
            logger.error(f"Failed to load package {package}: {e}")
            raise ProcessingError(f"Failed to load required package '{package}': {str(e)}")

    # Apply default enabled components
    apply_default_enabled(plugins)

    # Apply component-specific configurations
    apply_configs(plugins)

    logger.info(f"Successfully loaded packages: {', '.join(packages)}")
    logger.info("Insights-core components loading completed")
