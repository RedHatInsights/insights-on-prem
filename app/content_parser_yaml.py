"""
Parse rule content from markdown/YAML files.

This module reads rule metadata from the content/ directory structure
that matches the format used by insights-content-service.
"""

import logging
from pathlib import Path

import yaml

from app.exceptions import ProcessingError

logger = logging.getLogger(__name__)


class YAMLContentParser:
    """Parser for extracting rule metadata from markdown/YAML content files."""

    def __init__(self, content_path: str = None):
        """
        Initialize the parser.

        :param content_path: Path to rules-content directory. If None, use default.
        """
        if content_path is None:
            # Default to rules-content in project root
            self.content_path = Path(__file__).parent.parent / "content"
        else:
            self.content_path = Path(content_path)

        if not self.content_path.exists():
            logger.error(f"Content path {self.content_path} does not exist")
            raise ProcessingError(
                f"Rules content directory not found: {self.content_path}"
            )

        # Load impact mapping from config.yaml
        self.impact_mapping = self._load_impact_mapping()

    def _load_impact_mapping(self) -> dict:
        """
        Load impact name to numeric value mapping from config.yaml.

        :return: Dictionary mapping impact names to numeric values (1-4)
        """
        config_file = self.content_path / "config.yaml"
        if not config_file.exists():
            logger.warning(f"Config file {config_file} not found")
            return {}

        try:
            with open(config_file, encoding="utf-8") as f:
                config = yaml.safe_load(f)
                impact_map = config.get("impact", {})
                logger.debug(f"Loaded {len(impact_map)} impact mappings from config")
                return impact_map
        except Exception as e:
            logger.warning(f"Failed to load impact mapping: {e}")
            return {}

    def parse_all_rules(self) -> list[dict]:
        """
        Parse all rule files and extract metadata.

        :return: List of rule content dictionaries
        """
        rules_content = []

        # Parse external rules - scan all subdirectories (rules, ocs, dvo, etc.)
        external_path = self.content_path / "external"
        if external_path.exists():
            for subdir in external_path.iterdir():
                if subdir.is_dir() and not subdir.name.startswith("."):
                    # e.g., external/rules, external/ocs, external/dvo
                    rules_content.extend(
                        self._parse_rules_directory(subdir, f"external.{subdir.name}")
                    )

        # Parse internal rules - scan all subdirectories
        internal_path = self.content_path / "internal"
        if internal_path.exists():
            for subdir in internal_path.iterdir():
                if subdir.is_dir() and not subdir.name.startswith("."):
                    # e.g., internal/rules
                    rules_content.extend(
                        self._parse_rules_directory(subdir, f"internal.{subdir.name}")
                    )

        logger.info(
            f"Parsed {len(rules_content)} rule content entries from {self.content_path}"
        )
        return rules_content

    def _parse_rules_directory(self, rules_dir: Path, rule_type: str) -> list[dict]:
        """
        Parse all rules in a directory (external or internal).

        :param rules_dir: Path to rules directory
        :param rule_type: Type of rules (external/internal)
        :return: List of rule content dictionaries
        """
        rules_content = []

        # Each subdirectory is a rule
        for rule_dir in rules_dir.iterdir():
            if not rule_dir.is_dir() or rule_dir.name.startswith("."):
                continue

            try:
                rule_content = self._parse_rule_directory(rule_dir, rule_type)
                if rule_content:
                    rules_content.extend(rule_content)
            except Exception as e:
                logger.warning(f"Failed to parse {rule_dir.name}: {e}")

        return rules_content

    def _parse_rule_directory(self, rule_dir: Path, rule_type: str) -> list[dict]:
        """
        Parse a single rule directory.

        :param rule_dir: Path to rule directory
        :param rule_type: Type of rule (e.g., "external.rules", "external.ocs")
        :return: List of rule content dictionaries (one per error key)
        """
        rule_name = rule_dir.name
        # rule_type already includes the subdirectory (e.g., "external.ocs")
        module_name = f"ccx_rules_ocp.{rule_type}.{rule_name}"

        # Read plugin.yaml
        plugin_file = rule_dir / "plugin.yaml"
        if not plugin_file.exists():
            logger.warning(f"No plugin.yaml found for {rule_name}")
            return []

        # Read plugin-level markdown files (used as fallback for error keys)
        plugin_content = self._read_markdown_files(rule_dir)

        # Find all error key directories
        error_key_dirs = [
            d for d in rule_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
        ]

        rules = []
        for error_key_dir in error_key_dirs:
            error_key = error_key_dir.name

            try:
                content = self._parse_error_key_directory(error_key_dir, plugin_content)

                # Merge with plugin-level metadata
                # Handle impact - can be a string or int in metadata
                impact_value = content.get("impact", 1)
                if isinstance(impact_value, dict):
                    impact = impact_value.get("impact", 1)
                elif isinstance(impact_value, (int, float)):
                    impact = int(impact_value)
                else:
                    # String value - look up in config.yaml mapping
                    # Use exact string first, then try lowercase, default to 1
                    # (per config.yaml: "null: 1")
                    impact_str = str(impact_value)
                    impact = self.impact_mapping.get(
                        impact_str, self.impact_mapping.get(impact_str.lower(), 1)
                    )

                # Get likelihood
                likelihood = content.get("likelihood", 1)

                # Calculate total_risk using smart-proxy formula:
                # total_risk = (impact + likelihood) / 2
                # This matches content/parsing.go:calculateTotalRisk()
                total_risk = (impact + likelihood) // 2  # Integer division

                rule_content = {
                    "rule_fqdn": module_name,
                    "error_key": error_key,
                    "description": content.get("generic", ""),
                    "generic": content.get("generic", ""),
                    "reason": content.get("reason", ""),
                    "resolution": content.get("resolution", ""),
                    "more_info": content.get("more_info", ""),
                    "total_risk": total_risk,
                    "likelihood": likelihood,
                    "impact": impact,
                    "publish_date": content.get("publish_date", ""),
                    "tags": content.get("tags", []),  # Store as list
                }

                rules.append(rule_content)

            except Exception as e:
                logger.warning(
                    f"Failed to parse error key {error_key} for {rule_name}: {e}"
                )

        return rules

    def _read_markdown_files(self, directory: Path) -> dict:
        """
        Read markdown content files from a directory.

        :param directory: Path to directory containing markdown files
        :return: Dictionary mapping file type to content
        """
        content = {}
        for md_type in ["generic", "reason", "resolution", "more_info"]:
            md_file = directory / f"{md_type}.md"
            if md_file.exists():
                with open(md_file, encoding="utf-8") as f:
                    text = f.read().strip()
                    if text:
                        content[md_type] = text
        return content

    def _parse_error_key_directory(
        self, error_key_dir: Path, plugin_content: dict
    ) -> dict:
        """
        Parse an error key directory containing metadata and markdown files.

        Plugin-level markdown content is used as fallback when the error key
        directory does not contain the corresponding file.

        :param error_key_dir: Path to error key directory
        :param plugin_content: Plugin-level markdown content for fallback
        :return: Dictionary with content metadata
        """
        # Start with plugin-level markdown as defaults
        content = dict(plugin_content)

        # Read metadata.yaml if it exists
        metadata_file = error_key_dir / "metadata.yaml"
        if metadata_file.exists():
            with open(metadata_file, encoding="utf-8") as f:
                metadata = yaml.safe_load(f)
                if metadata:
                    content.update(metadata)

        # Read error-key-level markdown files (override plugin-level)
        content.update(self._read_markdown_files(error_key_dir))

        return content
