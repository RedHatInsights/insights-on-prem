"""Tests for ThanosService."""

from unittest.mock import mock_open, patch

import httpx
import pytest
from app.config import AppConfig
from app.services.thanos_service import ThanosService


@pytest.fixture
def config():
    return AppConfig(
        thanos_url="https://thanos.test:8443",
        thanos_query_timeout_seconds=5,
        thanos_query_lookback_minutes=60,
    )


@pytest.fixture
def thanos_service(config):
    return ThanosService(config)


def test_build_query(thanos_service):
    """Test PromQL query construction."""
    query = thanos_service._build_query("test-cluster-123")
    assert 'console_url{clusterID=~"test-cluster-123"}' in query
    assert 'ALERTS{clusterID=~"test-cluster-123"' in query
    assert 'namespace=~"openshift-.*"' in query
    assert 'severity=~"warning|critical"' in query
    assert (
        'cluster_operator_conditions{clusterID=~"test-cluster-123", condition="Available"} == 0'
        in query
    )
    assert (
        'cluster_operator_conditions{clusterID=~"test-cluster-123", condition="Degraded"} == 1'
        in query
    )


def test_parse_response_console_url(thanos_service):
    """Test extracting console_url from Prometheus response."""
    data = {
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {
                        "__name__": "console_url",
                        "clusterID": "cluster-1",
                        "url": "https://console.example.com",
                    },
                    "value": [1677825120.237, "1"],
                }
            ],
        }
    }
    console_url, alerts, focs = thanos_service._parse_response(data)
    assert console_url == "https://console.example.com"
    assert alerts == []
    assert focs == []


def test_parse_response_alerts(thanos_service):
    """Test extracting alerts from Prometheus response."""
    data = {
        "data": {
            "result": [
                {
                    "metric": {
                        "__name__": "ALERTS",
                        "clusterID": "cluster-1",
                        "alertname": "KubeAPIDown",
                        "namespace": "openshift-kube-apiserver",
                        "severity": "critical",
                    },
                    "value": [1677825120.237, "1"],
                },
                {
                    "metric": {
                        "__name__": "ALERTS",
                        "clusterID": "cluster-1",
                        "alertname": "NodeDown",
                        "namespace": "openshift-monitoring",
                        "severity": "warning",
                    },
                    "value": [1677825120.237, "1"],
                },
            ]
        }
    }
    console_url, alerts, focs = thanos_service._parse_response(data)
    assert console_url == ""
    assert len(alerts) == 2
    assert alerts[0].name == "KubeAPIDown"
    assert alerts[0].namespace == "openshift-kube-apiserver"
    assert alerts[0].severity == "critical"
    assert alerts[1].name == "NodeDown"
    assert alerts[1].severity == "warning"


def test_parse_response_focs(thanos_service):
    """Test extracting operator conditions from Prometheus response."""
    data = {
        "data": {
            "result": [
                {
                    "metric": {
                        "__name__": "cluster_operator_conditions",
                        "clusterID": "cluster-1",
                        "condition": "Available",
                        "name": "authentication",
                        "reason": "EndpointUnavailable",
                    },
                    "value": [1677825120.237, "0"],
                },
                {
                    "metric": {
                        "__name__": "cluster_operator_conditions",
                        "clusterID": "cluster-1",
                        "condition": "Degraded",
                        "name": "dns",
                        "reason": "DNSError",
                    },
                    "value": [1677825120.237, "1"],
                },
            ]
        }
    }
    console_url, alerts, focs = thanos_service._parse_response(data)
    assert len(focs) == 2
    assert focs[0].name == "authentication"
    assert focs[0].condition == "Not Available"
    assert focs[0].reason == "EndpointUnavailable"
    assert focs[1].name == "dns"
    assert focs[1].condition == "Degraded"


def test_parse_response_mixed(thanos_service):
    """Test parsing a response with all metric types."""
    data = {
        "data": {
            "result": [
                {
                    "metric": {
                        "__name__": "console_url",
                        "url": "https://console.example.com",
                    },
                    "value": [1677825120.237, "1"],
                },
                {
                    "metric": {
                        "__name__": "ALERTS",
                        "alertname": "KubeAPIDown",
                        "namespace": "openshift-kube-apiserver",
                        "severity": "critical",
                    },
                    "value": [1677825120.237, "1"],
                },
                {
                    "metric": {
                        "__name__": "cluster_operator_conditions",
                        "condition": "Degraded",
                        "name": "dns",
                    },
                    "value": [1677825120.237, "1"],
                },
            ]
        }
    }
    console_url, alerts, focs = thanos_service._parse_response(data)
    assert console_url == "https://console.example.com"
    assert len(alerts) == 1
    assert len(focs) == 1


def test_parse_response_empty(thanos_service):
    """Test parsing an empty response."""
    data = {"data": {"result": []}}
    console_url, alerts, focs = thanos_service._parse_response(data)
    assert console_url == ""
    assert alerts == []
    assert focs == []


def test_parse_response_skips_missing_metric(thanos_service):
    """Test that results without a metric dict are skipped."""
    data = {"data": {"result": [{"value": [1677825120.237, "1"]}]}}
    console_url, alerts, focs = thanos_service._parse_response(data)
    assert console_url == ""
    assert alerts == []
    assert focs == []


def test_parse_response_console_url_without_url_field(thanos_service):
    """Test console_url metric without url field is ignored."""
    data = {
        "data": {
            "result": [
                {
                    "metric": {"__name__": "console_url", "clusterID": "cluster-1"},
                    "value": [1677825120.237, "1"],
                }
            ]
        }
    }
    console_url, alerts, focs = thanos_service._parse_response(data)
    assert console_url == ""


@patch("app.services.thanos_service.httpx.get")
@patch("builtins.open", mock_open(read_data="test-token"))
def test_query_cluster_metrics(mock_get, thanos_service):
    """Test full query flow with mocked HTTP."""
    mock_request = httpx.Request("GET", "https://thanos.test:8443/api/v1/query")
    mock_response = httpx.Response(
        200,
        json={
            "data": {
                "result": [
                    {
                        "metric": {
                            "__name__": "console_url",
                            "url": "https://console.example.com",
                        },
                        "value": [1677825120.237, "1"],
                    },
                    {
                        "metric": {
                            "__name__": "ALERTS",
                            "alertname": "TestAlert",
                            "namespace": "openshift-test",
                            "severity": "critical",
                        },
                        "value": [1677825120.237, "1"],
                    },
                ]
            }
        },
        request=mock_request,
    )
    mock_get.return_value = mock_response

    console_url, alerts, focs = thanos_service.query_cluster_metrics("cluster-123")

    assert console_url == "https://console.example.com"
    assert len(alerts) == 1
    assert alerts[0].name == "TestAlert"
    mock_get.assert_called_once()
    call_kwargs = mock_get.call_args
    assert "Bearer test-token" in call_kwargs.kwargs["headers"]["Authorization"]


@patch("app.services.thanos_service.httpx.get")
@patch("builtins.open", mock_open(read_data="test-token"))
def test_query_cluster_metrics_http_error(mock_get, thanos_service):
    """Test that HTTP errors are propagated."""
    mock_request = httpx.Request("GET", "https://thanos.test:8443/api/v1/query")
    mock_get.return_value = httpx.Response(
        500, text="Internal Server Error", request=mock_request
    )

    with pytest.raises(httpx.HTTPStatusError):
        thanos_service.query_cluster_metrics("cluster-123")
