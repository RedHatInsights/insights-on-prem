"""
In-process content service that mimics insights-content-service behavior.

This module provides content service functionality by reading markdown/YAML files
directly from the rules-content directory, matching the behavior of the Go-based
insights-content-service but implemented in Python.
"""
import logging
from typing import Dict, Optional

from app.content_parser_yaml import YAMLContentParser

logger = logging.getLogger(__name__)


class ContentService:
    """
    In-process content service.

    Mimics insights-content-service by serving rule metadata from
    markdown/YAML files. Loads all content at initialization and keeps
    it in memory, just like the Go-based content-service does.
    """

    def __init__(self, content_path: str = None):
        """
        Initialize content service.

        :param content_path: Path to rules-content directory
        """
        self.parser = YAMLContentParser(content_path)
        self._content_index: Dict[tuple, Dict] = {}
        self._all_content: list = []
        self._load_content()

    def _load_content(self):
        """Load all content from files into memory."""
        logger.info("Loading rule content from files...")
        all_rules = self.parser.parse_all_rules()

        # Build index by (rule_fqdn, error_key)
        for rule in all_rules:
            key = (rule["rule_fqdn"], rule["error_key"])
            self._content_index[key] = rule

        # Store list of all content
        self._all_content = all_rules

        logger.info(f"Loaded {len(self._content_index)} rules into memory")

    def get_content(self, rule_fqdn: str, error_key: str) -> Optional[Dict]:
        """
        Get content for a specific rule and error key.

        :param rule_fqdn: Fully qualified rule name
        :param error_key: Error key
        :return: Rule content dictionary or None if not found
        """
        key = (rule_fqdn, error_key)
        content = self._content_index.get(key)

        if not content:
            logger.warning(f"Content not found for {rule_fqdn}:{error_key}")

        return content

    def get_all_content_smart_proxy_format(self) -> list:
        """
        Get all rule content in smart-proxy format.

        :return: List of rules in smart-proxy nested format
        """
        # Group rules by python_module (rule_fqdn without the specific rule name)
        rules_by_module = {}

        for rule in self._all_content:
            rule_fqdn = rule["rule_fqdn"]
            error_key = rule["error_key"]

            # If this module doesn't exist yet, create it
            if rule_fqdn not in rules_by_module:
                rules_by_module[rule_fqdn] = {
                    "plugin": {
                        "name": "",
                        "node_id": "",
                        "product_code": "",
                        "python_module": rule_fqdn,
                    },
                    "error_keys": {},
                    "generic": rule.get("generic", ""),
                    "summary": "",
                    "resolution": rule.get("resolution", ""),
                    "more_info": rule.get("more_info", ""),
                    "reason": rule.get("reason", ""),
                    "HasReason": bool(rule.get("reason", "")),
                }

            # Add this error key to the module
            rules_by_module[rule_fqdn]["error_keys"][error_key] = {
                "metadata": {
                    "description": rule.get("description", ""),
                    "impact": rule.get("impact_string", self._impact_to_string(rule.get("impact", 1))),
                    "likelihood": rule.get("likelihood", 1),
                    "publish_date": rule.get("publish_date", ""),
                    "status": "active",
                    "tags": rule.get("tags", []),
                },
                "total_risk": rule.get("total_risk", 1),
                "generic": rule.get("generic", ""),
                "summary": "",
                "resolution": rule.get("resolution", ""),
                "more_info": rule.get("more_info", ""),
                "reason": rule.get("reason", ""),
                "HasReason": bool(rule.get("reason", "")),
            }

        return list(rules_by_module.values())

    @staticmethod
    def _impact_to_string(impact: int) -> str:
        """
        Convert numeric impact to string.

        :param impact: Numeric impact level (1-4)
        :return: String representation of impact level
        """
        impact_map = {
            1: "Low Impact",
            2: "Medium Impact",
            3: "High Impact",
            4: "Critical Impact",
        }
        return impact_map.get(impact, "Medium Impact")


# Global content service instance (initialized once)
_content_service: Optional[ContentService] = None


def get_content_service() -> ContentService:
    """
    Get or create the global content service instance.

    :return: ContentService instance
    """
    global _content_service
    if _content_service is None:
        _content_service = ContentService()
    return _content_service
