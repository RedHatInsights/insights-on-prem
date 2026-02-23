"""Service for querying Thanos/Prometheus metrics via rbac-query-proxy."""
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import httpx

from app.config import AppConfig

logger = logging.getLogger(__name__)

SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt"


@dataclass
class Alert:
    """Alert extracted from Prometheus metrics."""

    name: str
    namespace: Optional[str] = None
    severity: str = ""


@dataclass
class OperatorCondition:
    """Failing operator condition extracted from Prometheus metrics."""

    name: str
    condition: str
    reason: Optional[str] = None


class ThanosService:
    """Queries Thanos for cluster health metrics."""

    def __init__(self, config: AppConfig):
        self.thanos_url = config.thanos_url
        self.timeout = config.thanos_query_timeout
        self.lookback_minutes = config.thanos_query_lookback_minutes

    def _get_bearer_token(self) -> str:
        with open(SA_TOKEN_PATH) as f:
            return f.read().strip()

    def _build_query(self, cluster_id: str) -> str:
        return (
            f'console_url{{clusterID=~"{cluster_id}"}}'
            " or "
            f'ALERTS{{clusterID=~"{cluster_id}", namespace=~"openshift-.*", severity=~"warning|critical"}}'
            " or "
            f'cluster_operator_conditions{{clusterID=~"{cluster_id}", condition="Available"}} == 0'
            " or "
            f'cluster_operator_conditions{{clusterID=~"{cluster_id}", condition="Degraded"}} == 1'
        )

    def _parse_response(
        self, data: dict
    ) -> Tuple[str, List[Alert], List[OperatorCondition]]:
        console_url = ""
        alerts: List[Alert] = []
        operator_conditions: List[OperatorCondition] = []

        results = data.get("data", {}).get("result", [])

        for result in results:
            metric = result.get("metric")
            if not metric:
                continue

            name = metric.get("__name__")

            if name == "console_url":
                url = metric.get("url")
                if url:
                    console_url = url

            elif name == "ALERTS":
                alerts.append(
                    Alert(
                        name=metric.get("alertname", ""),
                        namespace=metric.get("namespace"),
                        severity=metric.get("severity", ""),
                    )
                )

            elif name == "cluster_operator_conditions":
                condition = metric.get("condition", "")
                if condition == "Available":
                    condition = "Not Available"

                operator_conditions.append(
                    OperatorCondition(
                        name=metric.get("name", ""),
                        condition=condition,
                        reason=metric.get("reason"),
                    )
                )

        return console_url, alerts, operator_conditions

    def query_cluster_metrics(
        self, cluster_id: str
    ) -> Tuple[str, List[Alert], List[OperatorCondition]]:
        """Query Thanos for alerts and operator conditions for a cluster.

        Returns (console_url, alerts, operator_conditions).
        """
        query = self._build_query(cluster_id)
        logger.info(query)
        query_time = (
            datetime.now() - timedelta(minutes=self.lookback_minutes)
        ).timestamp()

        token = self._get_bearer_token()

        response = httpx.get(
            f"{self.thanos_url}/api/v1/query",
            params={"query": query, "time": query_time},
            headers={"Authorization": f"Bearer {token}"},
            verify=SA_CA_PATH,
            timeout=self.timeout,
        )
        response.raise_for_status()

        return self._parse_response(response.json())
