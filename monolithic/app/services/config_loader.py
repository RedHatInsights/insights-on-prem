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

Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def load_insights_config(config_path: str = "config.yml") -> Dict:
    """
    Load insights-core configuration from YAML file.

    Args:
        config_path: Path to the YAML configuration file

    Returns:
        Configuration dictionary

    Raises:
        ProcessingError: If config file cannot be loaded
    """
    try:
        if not os.path.exists(config_path):
            logger.warning(f"Config file {config_path} not found, using defaults")
            return {
                "plugins": {"packages": [], "configs": []},
                "service": {
                    "extract_timeout": 300,
                    "extract_tmp_dir": settings.temp_upload_dir,
                    "format": "insights.formats._json.JsonFormat",
                    "target_components": [],
                    "unpacked_archive_size_limit": -1,
                },
                "logging": {},
            }

        with open(config_path, "r") as f:
            config = yaml.load(f, Loader=Loader)

        logger.info(f"Loaded insights-core configuration from {config_path}")
        return config

    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        raise ProcessingError(f"Configuration loading failed: {str(e)}")


def load_insights_components(config: Dict) -> None:
    """
    Load insights-core components based on configuration.

    Args:
        config: Configuration dictionary from YAML file
    """
    plugins = config.get("plugins", {})
    packages = plugins.get("packages", [])

    # Load each package using dr.load_components
    loaded_packages = []
    failed_packages = []

    for package in packages:
        try:
            logger.info(f"Loading package: {package}")
            dr.load_components(package, continue_on_error=False)
            loaded_packages.append(package)
        except ImportError as e:
            logger.warning(f"Package {package} not available: {e}")
            failed_packages.append(package)
        except Exception as e:
            logger.error(f"Failed to load package {package}: {e}")
            failed_packages.append(package)

    # Apply default enabled components
    apply_default_enabled(plugins)

    # Apply component-specific configurations
    apply_configs(plugins)

    if loaded_packages:
        logger.info(f"Successfully loaded packages: {', '.join(loaded_packages)}")
    if failed_packages:
        logger.warning(f"Failed to load packages: {', '.join(failed_packages)}")

    logger.info("Insights-core components loading completed")
