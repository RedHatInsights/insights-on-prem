"""FastAPI application for Insights On Premise."""

import asyncio
import contextlib
import json
import logging
import os
import ssl
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.config_loader import load_config, load_insights_components
from app.content_parser_yaml import YAMLContentParser
from app.database import get_db, init_db
from app.exceptions import ProcessorBusyError, ValidationError
from app.models import Report, RequestReport, RuleHit
from app.schemas import (
    BatchUpgradeRisksPredictionRequest,
    BatchUpgradeRisksPredictionResponse,
    ClusterPrediction,
    ErrorResponse,
    ReportResponseV2,
    RequestReportResponse,
    RequestStatusResponse,
    SimplifiedRuleHit,
    UploadResponse,
)
from app.services.archive_processor import ArchiveProcessor
from app.services.content_service import ContentService
from app.services.processor_service import ProcessorService
from app.services.report_service import ReportService
from app.services.thanos_service import ThanosService
from app.services.upgrade_prediction_service import UpgradePredictionService
from app.services.upload_service import UploadService

logger = logging.getLogger(__name__)

TLS_DIR = "/tls"
TLS_CERT = os.path.join(TLS_DIR, "tls.crt")
TLS_KEY = os.path.join(TLS_DIR, "tls.key")
CLIENT_CA_PATH = os.path.join(TLS_DIR, "client-ca", "ca.crt")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()

    # Ensure temp upload directory exists
    os.makedirs(config.temp_upload_dir, exist_ok=True)
    logger.info(f"Temp upload directory: {config.temp_upload_dir}")

    # Initialize database
    engine, session_factory = init_db(config.database_url)
    app.state.engine = engine
    app.state.session_factory = session_factory
    logger.info("Database initialized successfully")

    # Initialize processor config and components
    load_insights_components(config)

    processor_service = ProcessorService(config)
    archive_processor = ArchiveProcessor(
        processor_service,
        session_factory,
        queue_size=config.archive_queue_size,
    )
    archive_processor.start()
    app.state.upload_service = UploadService(config, archive_processor.queue)
    app.state.content_service = ContentService(YAMLContentParser())
    app.state.report_service = ReportService(app.state.content_service)
    app.state.thanos_service = ThanosService(config)
    app.state.upgrade_prediction_service = UpgradePredictionService()
    logger.info("All services initialized successfully")

    cleanup_task = asyncio.create_task(_cleanup_old_records(session_factory, config))

    yield

    logger.info("Shutting down: waiting for archive processor to drain...")
    drained = await asyncio.to_thread(archive_processor.stop, 270)
    if drained:
        logger.info("Archive processor stopped")
    else:
        logger.warning("Shutdown timeout: archive processor still running")

    cleanup_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await cleanup_task


async def _cleanup_old_records(session_factory, config):
    """Periodically delete old records from all tables."""
    while True:
        db = session_factory()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(
                hours=config.db_retention_hours
            )
            for model, label in [
                (Report, "reports"),
                (RuleHit, "rule hits"),
                (RequestReport, "request reports"),
            ]:
                try:
                    deleted = model.delete_older_than(db, cutoff)
                    if deleted:
                        logger.info(f"Cleaned up {deleted} old {label}")
                    db.commit()
                except Exception as e:
                    logger.error(f"DB cleanup failed: {e}")
                    try:
                        db.rollback()
                    except Exception as rollback_err:
                        logger.error(f"Rollback also failed: {rollback_err}")
        finally:
            db.close()

        await asyncio.sleep(config.db_cleanup_interval_minutes * 60)


# Create FastAPI app
app = FastAPI(
    title="Insights On-Premise",
    description="Red Hat Insights archive processing for on-premise deployment",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/")
async def root():
    """Root endpoint for health check."""
    return {
        "service": "insights-on-premise",
        "status": "running",
        "version": "1.0.0",
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.post(
    "/api/ingress/v1/upload",
    response_model=UploadResponse,
    status_code=202,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Internal Server Error"},
        503: {
            "model": ErrorResponse,
            "description": "Archive processor is busy; retry later",
        },
    },
)
async def upload_archive(
    request: Request,
    response: Response,
    file: UploadFile = File(...),  # noqa: B008
):
    """
    Upload and process Red Hat Insights archive.

    :param file: Uploaded archive file (tar, tar.gz, or tgz format)
    :return: UploadResponse with accepted status
    :raises HTTPException: On validation errors
    """
    upload_service: UploadService = request.app.state.upload_service

    request_id = str(uuid.uuid4())

    try:
        upload_response = await upload_service.process_upload(file, request_id)
        response.headers["x-rh-insights-request-id"] = request_id
        return upload_response

    except ValidationError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e),
        ) from e

    except ProcessorBusyError as e:
        raise HTTPException(
            status_code=503,
            detail=str(e),
            headers={
                "Retry-After": "60",
                "x-rh-insights-request-id": request_id,
            },
        ) from e

    except Exception as e:
        logger.error(f"Request {request_id}: Unexpected error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Internal server error during upload processing",
        ) from e


@app.get(
    "/api/v2/cluster/{cluster_id}/reports",
    response_model=ReportResponseV2,
    status_code=200,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Cluster report not found"},
        500: {"model": ErrorResponse, "description": "Internal Server Error"},
    },
)
async def get_cluster_report_v2(
    request: Request,
    cluster_id: str,
    db: Session = Depends(get_db),  # noqa: B008
):
    """
    Retrieve the latest report for a specific cluster (v2 endpoint).

    This endpoint returns the latest report for the given cluster ID,
    following the v2 API format used by insights-results-smart-proxy.

    :param cluster_id: Cluster UUID
    :param db: Database session
    :return: ReportResponseV2 with detailed report data
    :raises HTTPException: On not found or processing errors
    """
    report_service: ReportService = request.app.state.report_service

    try:
        report_v2 = report_service.get_cluster_report_v2(db, cluster_id)
        return ReportResponseV2(
            report=report_v2,
            status="ok",
        )

    except ValueError as e:
        # Report not found
        raise HTTPException(
            status_code=404,
            detail=str(e),
        ) from e

    except Exception as e:
        logger.error(
            f"Error fetching v2 report for cluster {cluster_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error while fetching cluster report",
        ) from e


@app.post(
    "/api/insights-results-aggregator/v2/upgrade-risks-prediction",
    response_model=BatchUpgradeRisksPredictionResponse,
    status_code=200,
)
async def upgrade_risks_prediction_batch(
    request: Request,
    body: BatchUpgradeRisksPredictionRequest,
):
    """
    Batch upgrade risks prediction matching the ccx-upgrades-data-eng API.

    Accepts { clusters: [...] } and returns { predictions: [...] }, matching
    the MultiClusterUpgradeApiResponse format that the ACM console expects.
    This allows redirecting the console's console.redhat.com URP call to this
    service via a simple URL swap — no function patching required.

    :param body: Request body containing list of cluster UUIDs
    :return: BatchUpgradeRisksPredictionResponse
    """
    thanos_service: ThanosService = request.app.state.thanos_service
    prediction_service: UpgradePredictionService = (
        request.app.state.upgrade_prediction_service
    )

    max_batch_size = 100
    if len(body.clusters) > max_batch_size:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Batch size {len(body.clusters)} exceeds maximum of "
                f"{max_batch_size} clusters per request."
            ),
        )
    clusters = body.clusters

    async def predict_for_cluster(cluster_id: str) -> ClusterPrediction:
        try:
            console_url, alerts, focs = await asyncio.to_thread(
                thanos_service.query_cluster_metrics, cluster_id
            )
            result = prediction_service.predict(alerts, focs, console_url)
            return ClusterPrediction(
                cluster_id=cluster_id,
                prediction_status="ok",
                upgrade_recommended=result.upgrade_recommended,
                upgrade_risks_predictors=result.upgrade_risks_predictors,
                last_checked_at=datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            )
        except Exception:
            logger.exception(
                "Error predicting upgrade risks for cluster %s", cluster_id
            )
            return ClusterPrediction(
                cluster_id=cluster_id,
                prediction_status="No data for the cluster",
            )

    predictions = await asyncio.gather(*[predict_for_cluster(c) for c in clusters])
    return BatchUpgradeRisksPredictionResponse(predictions=list(predictions))


@app.get(
    "/api/v2/cluster/{cluster_id}/request/{request_id}/status",
    response_model=RequestStatusResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse, "description": "Request ID not found"},
    },
)
async def get_request_status(
    cluster_id: str,
    request_id: str,
    db: Session = Depends(get_db),  # noqa: B008
):
    """
    Check the processing status of an on-demand data gathering request.

    Returns 404 if the request has not been processed yet (operator retries).
    """
    record = RequestReport.get_by_cluster_and_request(db, cluster_id, request_id)
    if not record:
        raise HTTPException(
            status_code=404,
            detail="Request ID not found for given cluster_id",
        )

    return RequestStatusResponse(
        cluster=cluster_id,
        requestID=request_id,
        status="processed",
    )


@app.get(
    "/api/v2/cluster/{cluster_id}/request/{request_id}/report",
    response_model=RequestReportResponse,
    status_code=200,
    responses={
        404: {"model": ErrorResponse, "description": "Request ID not found"},
    },
)
async def get_request_report(
    request: Request,
    cluster_id: str,
    request_id: str,
    db: Session = Depends(get_db),  # noqa: B008
):
    """
    Retrieve the simplified report for an on-demand data gathering request.
    The raw report from DB does not include rules content, so the endpoint
    also repopulates with that (description and total risk).

    Returns 404 if the request has not been processed yet.
    """
    record = RequestReport.get_by_cluster_and_request(db, cluster_id, request_id)
    if not record:
        raise HTTPException(
            status_code=404,
            detail="Request ID not found for given cluster_id",
        )

    content_service = request.app.state.content_service

    try:
        rule_hits_raw = json.loads(record.report)
    except (json.JSONDecodeError, TypeError):
        rule_hits_raw = []

    rule_hits = []
    for hit in rule_hits_raw:
        rule_fqdn = hit.get("rule_fqdn", "")
        error_key = hit.get("error_key", "")
        content = content_service.get_content(rule_fqdn, error_key)
        if not content:
            logger.warning(
                f"Content not found for rule {rule_fqdn} error_key {error_key}, skipping"
            )
            continue
        rule_hits.append(
            SimplifiedRuleHit(
                rule_fqdn=rule_fqdn,
                error_key=error_key,
                description=content.get("description", ""),
                total_risk=content.get("total_risk", 0),
            )
        )

    return RequestReportResponse(
        cluster=cluster_id,
        requestID=request_id,
        status="processed",
        report=rule_hits,
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    """
    Custom handler for HTTP exceptions.

    :param request: HTTP request object
    :param exc: HTTPException instance
    :return: JSONResponse with error details
    """
    request_id = request.headers.get("x-rh-insights-request-id")
    if not request_id and exc.headers:
        request_id = exc.headers.get("x-rh-insights-request-id")
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=exc.detail,
            request_id=request_id,
        ).model_dump(),
        headers=exc.headers,
    )


def start_server():
    """Start uvicorn — plain HTTP or mTLS depending on MTLS_ENABLED."""
    config = load_config()

    if not config.mtls_enabled:
        uvi_config = uvicorn.Config(app, host="0.0.0.0", port=8000)
        uvicorn.Server(uvi_config).run()
        return

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
    ssl_context.load_cert_chain(TLS_CERT, TLS_KEY)
    ssl_context.load_verify_locations(cafile=CLIENT_CA_PATH)
    ssl_context.verify_mode = ssl.CERT_REQUIRED

    app.state.ssl_context = ssl_context

    uvi_config = uvicorn.Config(app, host="0.0.0.0", port=8443)
    uvi_config.load()
    uvi_config.ssl = ssl_context

    uvicorn.Server(uvi_config).run()


if __name__ == "__main__":
    start_server()
