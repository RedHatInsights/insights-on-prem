"""Utilities for building API responses."""
from typing import Dict, Optional

from app.schemas import RuleHitDetailedResponse
from app.utils.content import format_datetime_rfc3339


class ResponseBuilder:
    """Helper class for building API responses."""

    @staticmethod
    def build_rule_hit_v2(
        hit,
        content_data: Dict,
        insights_details: Dict,
        publish_date: Optional[str] = None,
    ) -> RuleHitDetailedResponse:
        """
        Build v2 rule hit detailed response.

        :param hit: RuleHit model instance
        :param content_data: Content data from ContentService
        :param insights_details: Details from insights-core report
        :param publish_date: Optional publish date string
        :return: RuleHitDetailedResponse for v2 API
        """
        # Use publish_date from content as created_at
        if publish_date:
            try:
                from datetime import datetime as dt
                if isinstance(publish_date, str):
                    pub_dt = dt.fromisoformat(publish_date.replace("Z", "+00:00"))
                else:
                    pub_dt = publish_date
                created_at = pub_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except:
                created_at = format_datetime_rfc3339(hit.updated_at)
        else:
            created_at = format_datetime_rfc3339(hit.updated_at)

        impacted = format_datetime_rfc3339(hit.updated_at)

        # Build extra_data by merging insights details
        extra_data = dict(insights_details)
        extra_data["error_key"] = hit.error_key
        extra_data["type"] = "rule"

        return RuleHitDetailedResponse(
            rule_id=hit.rule_fqdn,
            created_at=created_at,
            description=content_data.get("description", ""),
            details=content_data.get("generic", ""),
            reason=content_data.get("reason", ""),
            resolution=content_data.get("resolution", ""),
            more_info=content_data.get("more_info", ""),
            total_risk=content_data.get("total_risk", 1),
            disabled=False,
            disable_feedback="",
            disabled_at="",
            internal=False,
            user_vote=0,
            extra_data=extra_data,
            tags=content_data.get("tags", []),
            impacted=impacted,
        )
