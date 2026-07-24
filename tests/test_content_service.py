"""Tests for ContentService."""

from unittest.mock import Mock

from app.services.content_service import ContentService


def test_content_service_get_content_found():
    """Test getting content that exists."""
    mock_parser = Mock()
    test_rule = {
        "rule_fqdn": "test.rule",
        "error_key": "TEST_ERROR",
        "description": "Test description",
        "resolution": "Fix it",
    }
    mock_parser.parse_all_rules.return_value = [test_rule]

    service = ContentService(mock_parser)

    content = service.get_content("test.rule", "TEST_ERROR")

    assert content is not None
    assert content["rule_fqdn"] == "test.rule"
    assert content["error_key"] == "TEST_ERROR"
    assert content["description"] == "Test description"
    assert content["resolution"] == "Fix it"


def test_content_service_get_content_not_found():
    """Test getting content that doesn't exist returns None."""
    mock_parser = Mock()
    mock_parser.parse_all_rules.return_value = [
        {"rule_fqdn": "rule1", "error_key": "ERROR1"}
    ]

    service = ContentService(mock_parser)

    content = service.get_content("nonexistent.rule", "NONEXISTENT_ERROR")

    assert content is None


def test_content_service_get_content_different_error_keys():
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

    service = ContentService(mock_parser)

    content1 = service.get_content("test.rule", "ERROR1")
    assert content1["description"] == "First error"

    content2 = service.get_content("test.rule", "ERROR2")
    assert content2["description"] == "Second error"
