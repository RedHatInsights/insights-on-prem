"""Service for querying Thanos (deployed by Multicluster Observability Operator)
metrics via rbac-query-proxy."""
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import httpx

from app.config import AppConfig

logger = logging.getLogger(__name__)

_CLUSTER_METRICS_QUERY_TEMPLATE = (
    'console_url{{clusterID=~"{cluster_id}"}}'
    " or "
    'ALERTS{{clusterID=~"{cluster_id}", namespace=~"openshift-.*", severity=~"warning|critical"}}'
    " or "
    'cluster_operator_conditions{{clusterID=~"{cluster_id}", condition="Available"}} == 0'
    " or "
    'cluster_operator_conditions{{clusterID=~"{cluster_id}", condition="Degraded"}} == 1'
)


@dataclass
class Alert:
    """Alert extracted from Thanos metrics."""

    name: str
    namespace: Optional[str] = None
    severity: str = ""


@dataclass
class OperatorCondition:
    """Operator condition extracted from Thanos metrics."""

    name: str
    condition: str
    reason: Optional[str] = None


class ThanosService:
    """Queries Thanos in ACM for cluster health metrics."""

    def __init__(self, config: AppConfig):
        self.thanos_url = config.thanos_url
        self.timeout_seconds = config.thanos_query_timeout_seconds
        self.lookback_minutes = config.thanos_query_lookback_minutes
        self.sa_cert_path = config.thanos_sa_cert_path
        self.token_path = config.thanos_token_path

    def _get_bearer_token(self) -> str:
        with open(self.token_path) as f:
            return f.read().strip()

    def _build_query(self, cluster_id: str) -> str:
        """
        Builds a query for retrieving alert and operator conditions data.
        The query is slightly different from the one used in c.r.c.
        due to differences between Thanos (queried here) and RHOBS.

        :return: query string for Thanos
        """
        return _CLUSTER_METRICS_QUERY_TEMPLATE.format(cluster_id=cluster_id)

    def _parse_response(
        self, data: dict
    ) -> Tuple[str, List[Alert], List[OperatorCondition]]:
        """
        Parse raw response from Thanos API.

        :return: console_url, alerts, operator_conditions
        """
        console_url = ""
        alerts: List[Alert] = []
        operator_conditions: List[OperatorCondition] = []

        results = data.get("data", {}).get("result", [])

        for result in results:
            metric = result.get("metric")
            if not metric:
                continue

            name = metric.get("__name__")

            match name:  
                case "console_url":
                    url = metric.get("url")
                    if url:
                        console_url = url
                case "ALERTS":
                    alerts.append(
                        Alert(
                            name=metric.get("alertname", ""),
                            namespace=metric.get("namespace"),
                            severity=metric.get("severity", ""),
                        )
                    )
                case "cluster_operator_conditions":
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
        """
        Query Thanos for alerts and operator conditions for a cluster.

        :return: console_url, alerts, operator_conditions
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
            verify=self.sa_cert_path,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()

        return self._parse_response(response.json())
