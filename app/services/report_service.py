"""Service for report and rule hit business logic."""

import json
import logging

from sqlalchemy.orm import Session

from app.models import Report, RuleHit
from app.schemas import ReportMetaV2, ReportV2, RuleHitDetailedResponse
from app.services.content_service import ContentService
from app.utils.content import format_datetime_rfc3339, normalize_rule_fqdn
from app.utils.response_builder import ResponseBuilder

logger = logging.getLogger(__name__)


class ReportService:
    """Service providing cluster reports and rule hits data."""

    def __init__(self, content_service: ContentService):
        """
        Initialize the report service.

        :param content_service: Content service instance
        """
        self.content_service = content_service

    def get_cluster_report_v2(self, db: Session, cluster_id: str) -> ReportV2:
        """
        Fetch v2 format report for a specific cluster.

        :param db: Database session
        :param cluster_id: Cluster UUID
        :return: ReportV2 object
        :raises ValueError: If cluster report not found
        """
        logger.info(f"Fetching v2 report for cluster {cluster_id}")

        # Query the report for this cluster
        report = db.query(Report).filter(Report.cluster == cluster_id).first()

        if not report:
            logger.warning(f"Report not found for cluster {cluster_id}")
            raise ValueError(f"Cluster report not found for cluster {cluster_id}")

        # Parse the stored report to get insights-core results
        try:
            report_json = json.loads(report.report) if report.report else {}
            insights_results = json.loads(report_json.get("results", "{}"))
            insights_reports = insights_results.get("reports", [])
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse insights-core results: {e}")
            insights_reports = []

        # Create a lookup map for insights-core reports by component+key
        insights_map = {}
        for ir in insights_reports:
            component = ir.get("component", "")
            key = ir.get("key", "")
            insights_map[f"{component}|{key}"] = ir

        # Query rule hits for this cluster
        rule_hits = db.query(RuleHit).filter(RuleHit.cluster_id == cluster_id).all()

        # Build detailed rule hits response
        rule_hits_detailed = self._build_rule_hits_v2(rule_hits, insights_map)

        # Build metadata
        last_checked_at = format_datetime_rfc3339(report.last_checked_at)
        gathered_at = format_datetime_rfc3339(report.gathered_at)

        meta = ReportMetaV2(
            cluster_name=cluster_id,
            managed=False,
            count=len(rule_hits_detailed),
            last_checked_at=last_checked_at,
            gathered_at=gathered_at,
        )

        # Build report response
        report_v2 = ReportV2(
            meta=meta,
            data=rule_hits_detailed,
        )

        logger.info(
            f"Successfully fetched v2 report for cluster {cluster_id} "
            f"with {len(rule_hits_detailed)} rule hits"
        )

        return report_v2

    def _build_rule_hits_v2(
        self, rule_hits: list[RuleHit], insights_map: dict
    ) -> list[RuleHitDetailedResponse]:
        """
        Build v2 detailed rule hit responses.

        :param rule_hits: List of RuleHit model instances
        :param insights_map: Map of insights-core reports by component|key
        :return: List of RuleHitDetailedResponse objects
        """
        rule_hits_detailed = []

        for hit in rule_hits:
            # Strip .report suffix from rule_fqdn for content lookup
            rule_fqdn_for_content = normalize_rule_fqdn(hit.rule_fqdn)

            # Get content from content service
            content_data = self.content_service.get_content(
                rule_fqdn_for_content, hit.error_key
            )

            if content_data:
                # Get insights-core details for this rule hit
                insights_key = f"{hit.rule_fqdn}|{hit.error_key}"
                insights_report = insights_map.get(insights_key, {})
                insights_details = insights_report.get("details", {})

                # Build rule hit response using ResponseBuilder
                rule_hit_detailed = ResponseBuilder.build_rule_hit_v2(
                    hit,
                    content_data,
                    insights_details,
                    content_data.get("publish_date"),
                )

                rule_hits_detailed.append(rule_hit_detailed)
            else:
                logger.warning(
                    f"Content not found for rule {hit.rule_fqdn}, error_key {hit.error_key}"
                )

        return rule_hits_detailed
