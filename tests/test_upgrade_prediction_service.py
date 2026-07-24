"""Tests for UpgradePredictionService."""

import pytest
from app.services.thanos_service import Alert, OperatorCondition
from app.services.upgrade_prediction_service import UpgradePredictionService


@pytest.fixture
def service():
    return UpgradePredictionService()


@pytest.mark.parametrize(
    "alerts,focs,expected_upgrade_recommended,expected_alert_count,expected_foc_count,expected_foc_condition",
    [
        # Alert-based test cases
        ([], [], True, 0, 0, None),
        (
            [
                Alert(
                    name="KubeAPIDown",
                    namespace="openshift-kube-apiserver",
                    severity="critical",
                )
            ],
            [],
            True,
            0,
            0,
            None,
        ),
        (
            [
                Alert(
                    name="KubeAPIDown",
                    namespace="openshift-kube-apiserver",
                    severity="critical",
                ),
                Alert(name="EtcdDown", namespace="openshift-etcd", severity="critical"),
            ],
            [],
            False,
            2,
            0,
            None,
        ),
        (
            [
                Alert(
                    name="Alert1", namespace="openshift-monitoring", severity="warning"
                ),
                Alert(
                    name="Alert2", namespace="openshift-monitoring", severity="warning"
                ),
                Alert(
                    name="Alert3", namespace="openshift-monitoring", severity="warning"
                ),
            ],
            [],
            True,
            0,
            0,
            None,
        ),
        (
            [
                Alert(name="Alert1", namespace="openshift-cnv", severity="critical"),
                Alert(
                    name="Alert2", namespace="openshift-storage", severity="critical"
                ),
                Alert(
                    name="Alert3", namespace="openshift-logging", severity="critical"
                ),
            ],
            [],
            True,
            0,
            0,
            None,
        ),
        (
            [
                Alert(name="Alert1", namespace="kube-system", severity="critical"),
                Alert(name="Alert2", namespace="default", severity="critical"),
            ],
            [],
            True,
            0,
            0,
            None,
        ),
        (
            [
                Alert(name="Alert1", namespace=None, severity="critical"),
                Alert(name="Alert2", namespace=None, severity="critical"),
            ],
            [],
            True,
            0,
            0,
            None,
        ),
        # FOC-based test cases
        (
            [],
            [
                OperatorCondition(
                    name="authentication",
                    condition="Not Available",
                    reason="EndpointUnavailable",
                )
            ],
            False,
            0,
            1,
            "Not Available",
        ),
        (
            [],
            [OperatorCondition(name="dns", condition="Degraded", reason="DNSError")],
            False,
            0,
            1,
            "Degraded",
        ),
        (
            [],
            [OperatorCondition(name="dns", condition="Progressing")],
            True,
            0,
            0,
            None,
        ),
        # Mixed alerts and FOCs test case
        (
            [
                Alert(
                    name="KubeAPIDown",
                    namespace="openshift-kube-apiserver",
                    severity="critical",
                ),
                Alert(name="EtcdDown", namespace="openshift-etcd", severity="critical"),
            ],
            [OperatorCondition(name="authentication", condition="Not Available")],
            False,
            2,
            1,
            "Not Available",
        ),
    ],
    ids=[
        "no_alerts_or_focs",
        "single_critical_alert_not_enough",
        "two_critical_alerts_trigger_risk",
        "warning_alerts_filtered_out",
        "excluded_namespace_filtered",
        "non_openshift_namespace_filtered",
        "alert_without_namespace_filtered",
        "foc_not_available_triggers_risk",
        "foc_degraded_triggers_risk",
        "foc_other_condition_filtered",
        "mixed_alerts_and_focs_trigger_risk",
    ],
)
def test_predict_upgrade_risk_conditions(
    service,
    alerts,
    focs,
    expected_upgrade_recommended,
    expected_alert_count,
    expected_foc_count,
    expected_foc_condition,
):
    """Test various alert and operator condition filtering and risk triggering conditions."""
    result = service.predict(alerts, focs, "https://console.example.com")
    assert result.upgrade_recommended is expected_upgrade_recommended
    assert len(result.upgrade_risks_predictors.alerts) == expected_alert_count
    assert (
        len(result.upgrade_risks_predictors.operator_conditions) == expected_foc_count
    )
    assert result.status == "ok"
    if expected_foc_condition is not None:
        assert (
            result.upgrade_risks_predictors.operator_conditions[0].condition
            == expected_foc_condition
        )


def test_alert_console_url(service):
    """Test that alert URLs are built correctly."""
    alerts = [
        Alert(
            name="KubeAPIDown",
            namespace="openshift-kube-apiserver",
            severity="critical",
        ),
        Alert(name="EtcdDown", namespace="openshift-etcd", severity="critical"),
    ]
    result = service.predict(alerts, [], "https://console.example.com")
    url = result.upgrade_risks_predictors.alerts[0].url
    assert "monitoring/alerts" in url
    assert "name=KubeAPIDown" in url


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
        Alert(
            name="KubeAPIDown",
            namespace="openshift-kube-apiserver",
            severity="critical",
        ),
        Alert(name="EtcdDown", namespace="openshift-etcd", severity="critical"),
    ]
    focs = [
        OperatorCondition(name="auth", condition="Not Available"),
    ]
    result = service.predict(alerts, focs, "")
    assert result.upgrade_risks_predictors.alerts[0].url is None
    assert result.upgrade_risks_predictors.operator_conditions[0].url is None
