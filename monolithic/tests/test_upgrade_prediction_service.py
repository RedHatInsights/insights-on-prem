"""Tests for UpgradePredictionService."""
import pytest

from app.services.thanos_service import Alert, OperatorCondition
from app.services.upgrade_prediction_service import UpgradePredictionService


@pytest.fixture
def service():
    return UpgradePredictionService()


def test_predict_no_risks(service):
    """Test prediction with no alerts or FOCs returns upgrade recommended."""
    result = service.predict([], [], "https://console.example.com")
    assert result.upgrade_recommended is True
    assert result.upgrade_risks_predictors.alerts == []
    assert result.upgrade_risks_predictors.operator_conditions == []


def test_predict_single_critical_alert_not_enough(service):
    """Test that a single critical alert is not enough (need >= 2)."""
    alerts = [
        Alert(name="KubeAPIDown", namespace="openshift-kube-apiserver", severity="critical"),
    ]
    result = service.predict(alerts, [], "https://console.example.com")
    assert result.upgrade_recommended is True
    assert result.upgrade_risks_predictors.alerts == []


def test_predict_two_critical_alerts(service):
    """Test that two critical alerts trigger risk."""
    alerts = [
        Alert(name="KubeAPIDown", namespace="openshift-kube-apiserver", severity="critical"),
        Alert(name="EtcdDown", namespace="openshift-etcd", severity="critical"),
    ]
    result = service.predict(alerts, [], "https://console.example.com")
    assert result.upgrade_recommended is False
    assert len(result.upgrade_risks_predictors.alerts) == 2


def test_predict_warning_alerts_filtered_out(service):
    """Test that warning-severity alerts are filtered out."""
    alerts = [
        Alert(name="Alert1", namespace="openshift-monitoring", severity="warning"),
        Alert(name="Alert2", namespace="openshift-monitoring", severity="warning"),
        Alert(name="Alert3", namespace="openshift-monitoring", severity="warning"),
    ]
    result = service.predict(alerts, [], "")
    assert result.upgrade_recommended is True
    assert result.upgrade_risks_predictors.alerts == []


def test_predict_excluded_namespace_filtered(service):
    """Test that alerts from excluded namespaces are filtered out."""
    alerts = [
        Alert(name="Alert1", namespace="openshift-cnv", severity="critical"),
        Alert(name="Alert2", namespace="openshift-storage", severity="critical"),
        Alert(name="Alert3", namespace="openshift-logging", severity="critical"),
    ]
    result = service.predict(alerts, [], "")
    assert result.upgrade_recommended is True
    assert result.upgrade_risks_predictors.alerts == []


def test_predict_non_openshift_namespace_filtered(service):
    """Test that alerts from non-openshift namespaces are filtered out."""
    alerts = [
        Alert(name="Alert1", namespace="kube-system", severity="critical"),
        Alert(name="Alert2", namespace="default", severity="critical"),
    ]
    result = service.predict(alerts, [], "")
    assert result.upgrade_recommended is True


def test_predict_alert_without_namespace_filtered(service):
    """Test that alerts without namespace are filtered out."""
    alerts = [
        Alert(name="Alert1", namespace=None, severity="critical"),
        Alert(name="Alert2", namespace=None, severity="critical"),
    ]
    result = service.predict(alerts, [], "")
    assert result.upgrade_recommended is True


def test_predict_foc_not_available(service):
    """Test that Not Available FOC triggers risk."""
    focs = [
        OperatorCondition(name="authentication", condition="Not Available", reason="EndpointUnavailable"),
    ]
    result = service.predict([], focs, "https://console.example.com")
    assert result.upgrade_recommended is False
    assert len(result.upgrade_risks_predictors.operator_conditions) == 1
    assert result.upgrade_risks_predictors.operator_conditions[0].condition == "Not Available"


def test_predict_foc_degraded(service):
    """Test that Degraded FOC triggers risk."""
    focs = [
        OperatorCondition(name="dns", condition="Degraded", reason="DNSError"),
    ]
    result = service.predict([], focs, "")
    assert result.upgrade_recommended is False
    assert len(result.upgrade_risks_predictors.operator_conditions) == 1


def test_predict_foc_other_condition_filtered(service):
    """Test that FOCs with other conditions are filtered out."""
    focs = [
        OperatorCondition(name="dns", condition="Progressing"),
    ]
    result = service.predict([], focs, "")
    assert result.upgrade_recommended is True
    assert result.upgrade_risks_predictors.operator_conditions == []


def test_predict_mixed_risks(service):
    """Test prediction with both alerts and FOCs."""
    alerts = [
        Alert(name="KubeAPIDown", namespace="openshift-kube-apiserver", severity="critical"),
        Alert(name="EtcdDown", namespace="openshift-etcd", severity="critical"),
    ]
    focs = [
        OperatorCondition(name="authentication", condition="Not Available"),
    ]
    result = service.predict(alerts, focs, "https://console.example.com")
    assert result.upgrade_recommended is False
    assert len(result.upgrade_risks_predictors.alerts) == 2
    assert len(result.upgrade_risks_predictors.operator_conditions) == 1


def test_alert_console_url(service):
    """Test that alert URLs are built correctly."""
    alerts = [
        Alert(name="KubeAPIDown", namespace="openshift-kube-apiserver", severity="critical"),
        Alert(name="EtcdDown", namespace="openshift-etcd", severity="critical"),
    ]
    result = service.predict(alerts, [], "https://console.example.com")
    url = result.upgrade_risks_predictors.alerts[0].url
    assert "monitoring/alerts" in url
    assert "alert-name=KubeAPIDown" in url


def test_foc_console_url(service):
    """Test that FOC URLs are built correctly."""
    focs = [
        OperatorCondition(name="authentication", condition="Not Available"),
    ]
    result = service.predict([], focs, "https://console.example.com")
    url = result.upgrade_risks_predictors.operator_conditions[0].url
    assert "ClusterOperator/authentication" in url


def test_urls_none_when_no_console_url(service):
    """Test that URLs are None when console_url is empty."""
    alerts = [
        Alert(name="KubeAPIDown", namespace="openshift-kube-apiserver", severity="critical"),
        Alert(name="EtcdDown", namespace="openshift-etcd", severity="critical"),
    ]
    focs = [
        OperatorCondition(name="auth", condition="Not Available"),
    ]
    result = service.predict(alerts, focs, "")
    assert result.upgrade_risks_predictors.alerts[0].url is None
    assert result.upgrade_risks_predictors.operator_conditions[0].url is None


def test_predict_status_field(service):
    """Test that response includes status field."""
    result = service.predict([], [], "")
    assert result.status == "ok"
