"""Service for predicting upgrade risks based on cluster metrics."""
import logging
from typing import List
from urllib.parse import urljoin

from app.schemas import (
    AlertResponse,
    OperatorConditionResponse,
    UpgradeRisksPredictors,
    UpgradeRisksPredictionResponse,
)
from app.services.thanos_service import Alert, OperatorCondition

logger = logging.getLogger(__name__)

EXCLUDE_NAMESPACES = [
    "openshift-cnv",
    "openshift-compliance",
    "openshift-operators",
    "openshift-storage",
    "openshift-logging",
    "openshift-gitops",
    "openshift-pipelines",
    "openshift-image-registry",
    "openshift-marketplace",
    "openshift-redhat-marketplace",
    "openshift-distributed-tracing",
    "openshift-gitlab-runner",
    "openshift-devspaces",
    "openshift-logs",
]


class UpgradePredictionService:
    """
    Service predicting cluster upgrade risk based on Thanos data
    about alerts and failing operator conditions. The service
    mimics the logic of upgrades-inference service used in data
    processing pipeline in c.r.c.
    """

    def _filter_alert(self, alert: Alert) -> bool:
        """
        Filters alerts only to critical and excludes certain namespaces.

        :return: true if alert should be included and false otherwise
        """
        if alert.namespace is None:
            return False
        return (
            alert.severity == "critical"
            and alert.namespace.startswith("openshift-")
            and alert.namespace not in EXCLUDE_NAMESPACES
        )

    def _filter_foc(self, foc: OperatorCondition) -> bool:
        """
        Filters operator conditions only to those that degraded or unavailable.

        :return: true if conditon should be included and false otherwise
        """
        return foc.condition in ["Not Available", "Degraded"]

    def _build_alert_url(self, console_url: str, alert_name: str) -> str:
        """
        Create an URL for each alert in the response.

        :return: full URL leading to alert details 
        """
        if not console_url or not alert_name:
            return ""
        return urljoin(
            console_url,
            f"/monitoring/alerts?name={alert_name}",
        )

    def _build_foc_url(self, console_url: str, operator_name: str) -> str:
        """
        Create an URL for each failing operator condition in the response.

        :return: full URL leading to FOC details 
        """
        if not console_url or not operator_name:
            return ""
        return urljoin(
            console_url,
            f"/k8s/cluster/config.openshift.io~v1~ClusterOperator/{operator_name}",
        )

    def predict(
        self,
        alerts: List[Alert],
        operator_conditions: List[OperatorCondition],
        console_url: str,
    ) -> UpgradeRisksPredictionResponse:
        """
        Filter alerts and FOCs to identify actual upgrade risks.
        
        :return: response object with prediction
        """
        filtered_alerts = [a for a in alerts if self._filter_alert(a)]
        filtered_focs = [f for f in operator_conditions if self._filter_foc(f)]

        # Require at least 2 critical alerts to consider them a risk
        if len(filtered_alerts) < 2:
            filtered_alerts = []

        alert_responses = [
            AlertResponse(
                name=alert.name,
                namespace=alert.namespace,
                severity=alert.severity,
                url=self._build_alert_url(console_url, alert.name) or None,
            )
            for alert in filtered_alerts
        ]

        foc_responses = [
            OperatorConditionResponse(
                name=foc.name,
                condition=foc.condition,
                reason=foc.reason,
                url=self._build_foc_url(console_url, foc.name) or None,
            )
            for foc in filtered_focs
        ]

        upgrade_recommended = len(alert_responses) == 0 and len(foc_responses) == 0

        return UpgradeRisksPredictionResponse(
            upgrade_recommended=upgrade_recommended,
            upgrade_risks_predictors=UpgradeRisksPredictors(
                alerts=alert_responses,
                operator_conditions=foc_responses,
            ),
        )
