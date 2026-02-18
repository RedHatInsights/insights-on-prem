"""Tests for ContentService."""
from unittest.mock import Mock, patch

import pytest

from app.services.content_service import ContentService


@patch("app.services.content_service.YAMLContentParser")
def test_content_service_get_content_found(mock_parser_class):
    """Test getting content that exists."""
    mock_parser = Mock()
    test_rule = {
        "rule_fqdn": "test.rule",
        "error_key": "TEST_ERROR",
        "description": "Test description",
        "resolution": "Fix it",
    }
    mock_parser.parse_all_rules.return_value = [test_rule]
    mock_parser_class.return_value = mock_parser

    service = ContentService("/path/to/content")

    # Get content
    content = service.get_content("test.rule", "TEST_ERROR")

    assert content is not None
    assert content["rule_fqdn"] == "test.rule"
    assert content["error_key"] == "TEST_ERROR"
    assert content["description"] == "Test description"
    assert content["resolution"] == "Fix it"


@patch("app.services.content_service.YAMLContentParser")
def test_content_service_get_content_not_found(mock_parser_class):
    """Test getting content that doesn't exist returns None."""
    mock_parser = Mock()
    mock_parser.parse_all_rules.return_value = [
        {"rule_fqdn": "rule1", "error_key": "ERROR1"}
    ]
    mock_parser_class.return_value = mock_parser

    service = ContentService("/path/to/content")

    # Try to get non-existent content
    content = service.get_content("nonexistent.rule", "NONEXISTENT_ERROR")

    assert content is None


@patch("app.services.content_service.YAMLContentParser")
def test_content_service_get_content_different_error_keys(mock_parser_class):
    """Test that different error keys for same rule are distinguished."""
    mock_parser = Mock()
    test_rules = [
        {
            "rule_fqdn": "test.rule",
            "error_key": "ERROR1",
            "description": "First error",
        },
        {
            "rule_fqdn": "test.rule",
            "error_key": "ERROR2",
            "description": "Second error",
        },
    ]
    mock_parser.parse_all_rules.return_value = test_rules
    mock_parser_class.return_value = mock_parser

    service = ContentService("/path/to/content")

    # Get first error
    content1 = service.get_content("test.rule", "ERROR1")
    assert content1["description"] == "First error"

    # Get second error
    content2 = service.get_content("test.rule", "ERROR2")
    assert content2["description"] == "Second error"


@patch("app.services.content_service.YAMLContentParser")
def test_content_service_with_no_path(mock_parser_class):
    """Test ContentService initialization with no path."""
    mock_parser = Mock()
    mock_parser.parse_all_rules.return_value = []
    mock_parser_class.return_value = mock_parser

    service = ContentService()

    # Verify parser was initialized with None
    mock_parser_class.assert_called_once_with(None)
