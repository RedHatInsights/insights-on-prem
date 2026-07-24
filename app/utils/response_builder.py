"""Utilities for building API responses."""

from datetime import datetime as dt

from app.schemas import RuleHitDetailedResponse
from app.utils.content import format_datetime_rfc3339

# Keys from content_data that map directly to RuleHitDetailedResponse fields
_CONTENT_FIELDS = {
    "description",
    "generic",
    "reason",
    "resolution",
    "more_info",
    "total_risk",
    "tags",
}


class ResponseBuilder:
    """Helper class for building API responses."""

    @staticmethod
    def build_rule_hit_v2(
        hit,
        content_data: dict,
        insights_details: dict,
        publish_date: str | None = None,
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
        try:
            pub_dt = dt.fromisoformat(publish_date.replace("Z", "+00:00"))
            created_at = pub_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:  # noqa: S110
            created_at = None

        # Build extra_data by merging insights details
        extra_data = dict(insights_details)
        extra_data["error_key"] = hit.error_key
        extra_data["type"] = "rule"

        return RuleHitDetailedResponse(
            rule_id=hit.rule_fqdn,
            created_at=created_at,
            extra_data=extra_data,
            impacted=format_datetime_rfc3339(hit.impacted_since),
            **{k: v for k, v in content_data.items() if k in _CONTENT_FIELDS},
        )
