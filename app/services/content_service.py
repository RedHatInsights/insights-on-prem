"""
In-process content service that mimics insights-content-service behavior.

This module provides content service functionality by reading markdown/YAML files
directly from the rules-content directory, matching the behavior of the Go-based
insights-content-service but implemented in Python.
"""

import logging

from app.content_parser_yaml import YAMLContentParser

logger = logging.getLogger(__name__)


class ContentService:
    """
    In-process content service.

    Mimics insights-content-service by serving rule metadata from
    markdown/YAML files. Loads all content at initialization and keeps
    it in memory, just like the Go-based content-service does.
    """

    def __init__(self, parser: YAMLContentParser):
        """
        Initialize content service.

        :param parser: Content parser instance
        """
        self.parser = parser
        self._content_index: dict[tuple, dict] = {}
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

    def get_content(self, rule_fqdn: str, error_key: str) -> dict | None:
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
