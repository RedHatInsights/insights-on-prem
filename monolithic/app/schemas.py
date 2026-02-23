"""Pydantic schemas for API request/response validation."""
from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    """Response schema for successful upload."""

    request_id: str = Field(..., description="Unique request identifier")
    status: str = Field(..., description="Processing status")
    uploaded_at: datetime = Field(..., description="Upload timestamp")


class ErrorResponse(BaseModel):
    """Response schema for errors."""

    error: str = Field(..., description="Error message")
    request_id: Optional[str] = Field(None, description="Request ID if available")
    detail: Optional[str] = Field(None, description="Additional error details")


# Schemas for upgrade risks prediction endpoint
class UpgradeRisksPredictionRequest(BaseModel):
    """Request body for upgrade risks prediction."""

    cluster_id: str = Field(..., description="Cluster UUID")


class AlertResponse(BaseModel):
    """Alert information from Thanos metrics."""

    name: str = Field(..., description="Alert name")
    namespace: Optional[str] = Field(None, description="Alert namespace")
    severity: str = Field(..., description="Alert severity")
    url: Optional[str] = Field(None, description="Console URL for the alert")


class OperatorConditionResponse(BaseModel):
    """Failing operator condition from Thanos metrics."""

    name: str = Field(..., description="Operator name")
    condition: str = Field(..., description="Condition type")
    reason: Optional[str] = Field(None, description="Condition reason")
    url: Optional[str] = Field(None, description="Console URL for the operator")


class UpgradeRisksPredictors(BaseModel):
    """Predictors that indicate upgrade risks."""

    alerts: List[AlertResponse] = Field(default_factory=list, description="Risky alerts")
    operator_conditions: List[OperatorConditionResponse] = Field(
        default_factory=list, description="Failing operator conditions"
    )


class UpgradeRisksPredictionResponse(BaseModel):
    """Response for upgrade risks prediction endpoint."""

    upgrade_recommended: bool = Field(..., description="Whether upgrade is recommended")
    upgrade_risks_predictors: UpgradeRisksPredictors = Field(
        ..., description="Detected upgrade risk predictors"
    )
    status: str = Field(default="ok", description="Response status")


# Schemas for v2 cluster report endpoint
class ReportMetaV2(BaseModel):
    """Metadata for v2 cluster report."""

    cluster_name: str = Field(..., description="Cluster UUID or name")
    managed: bool = Field(default=False, description="Whether cluster is managed")
    count: int = Field(..., description="Number of rules that hit")
    last_checked_at: Optional[str] = Field(None, description="Last check timestamp (RFC3339)")
    gathered_at: Optional[str] = Field(None, description="Data gathering timestamp (RFC3339)")


class RuleHitDetailedResponse(BaseModel):
    """Detailed rule hit response for v2 report (matching smart-proxy format)."""

    rule_id: str = Field(..., description="Rule module/FQDN")
    created_at: Optional[str] = Field(None, description="When rule was created (RFC3339)")
    description: str = Field(default="", description="Rule description")
    details: str = Field(default="", alias="generic", description="Generic/detailed information")
    reason: str = Field(default="", description="Reason for the rule hit")
    resolution: str = Field(default="", description="Resolution steps")
    more_info: str = Field(default="", description="Additional information URL")
    total_risk: int = Field(default=1, description="Total risk level (1-4)")
    disabled: bool = Field(default=False, description="Whether rule is disabled")
    disable_feedback: str = Field(default="", description="Feedback for disabled rule")
    disabled_at: Optional[str] = Field(default="", description="When rule was disabled")
    internal: bool = Field(default=False, description="Whether rule is internal")
    user_vote: int = Field(default=0, description="User vote (-1, 0, 1)")
    extra_data: Dict[str, Any] = Field(default_factory=dict, description="Additional template data")
    tags: List[str] = Field(default_factory=list, description="Rule tags")
    impacted: Optional[str] = Field(None, description="When cluster was impacted (RFC3339)")

    class Config:
        populate_by_name = True  # Allow both 'details' and 'generic' field names


class ReportV2(BaseModel):
    """Report data structure for v2 endpoint."""

    meta: ReportMetaV2 = Field(..., description="Report metadata")
    data: List[RuleHitDetailedResponse] = Field(default_factory=list, description="List of rule hits")


class ReportResponseV2(BaseModel):
    """Response schema for v2 cluster report endpoint."""

    report: ReportV2 = Field(..., description="Report data")
    status: str = Field(default="ok", description="Response status")
