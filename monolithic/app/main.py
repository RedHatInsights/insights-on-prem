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
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session
from watchfiles import awatch

from app.config_loader import load_config, load_insights_components
from app.content_parser_yaml import YAMLContentParser
from app.database import get_db, init_db
from app.exceptions import ValidationError
from app.models import RequestReport
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
CLIENT_CA_DIR = os.path.join(TLS_DIR, "client-ca")
CLIENT_CA_PATH = os.path.join(CLIENT_CA_DIR, "ca.crt")


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

    app.state.processor_service = ProcessorService(config)
    app.state.upload_service = UploadService(
        app.state.processor_service, config, session_factory
    )
    app.state.content_service = ContentService(YAMLContentParser())
    app.state.report_service = ReportService(app.state.content_service)
    app.state.thanos_service = ThanosService(config)
    app.state.upgrade_prediction_service = UpgradePredictionService()
    logger.info("All services initialized successfully")

    # Start periodic cleanup of old request reports
    cleanup_task = asyncio.create_task(
        _cleanup_old_request_reports(session_factory, config)
    )

    # Watch for certificate changes and hot-reload the SSLContext
    cert_watcher_task = None
    app.state.cert_reload_error = None
    if hasattr(app.state, "ssl_context"):
        cert_watcher_task = asyncio.create_task(_watch_certs(app))

    yield

    for task in (cleanup_task, cert_watcher_task):
        if task is None:
            continue
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _cleanup_old_request_reports(session_factory, config):
    """Periodically delete old request report records."""
    while True:
        db = session_factory()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(
                hours=config.request_report_retention_hours
            )
            deleted = RequestReport.delete_older_than(db, cutoff)
            db.commit()
            if deleted:
                logger.info(f"Cleaned up {deleted} old request reports")
        except Exception as e:
            logger.error(f"Request report cleanup failed: {e}")
            try:
                db.rollback()
            except Exception as rollback_err:
                logger.error(f"Rollback also failed: {rollback_err}")
        finally:
            db.close()

        # Wait for configured time until next cleanup
        await asyncio.sleep(config.request_report_cleanup_interval_minutes * 60)


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
async def health_check(request: Request):
    """Health check endpoint."""
    error = getattr(request.app.state, "cert_reload_error", None)
    if error:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "reason": error},
        )
    return {"status": "healthy"}


@app.post(
    "/api/ingress/v1/upload",
    response_model=UploadResponse,
    status_code=202,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Internal Server Error"},
    },
)
async def upload_archive(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),  # noqa: B008
):
    """
    Upload and process Red Hat Insights archive.

    :param file: Uploaded archive file (tar, tar.gz, or tgz format)
    :param background_tasks: FastAPI background tasks
    :return: UploadResponse with accepted status
    :raises HTTPException: On validation errors
    """
    upload_service: UploadService = request.app.state.upload_service

    request_id = str(uuid.uuid4())

    try:
        upload_response = await upload_service.process_upload(
            background_tasks, file, request_id
        )
        response.headers["x-rh-insights-request-id"] = request_id
        return upload_response

    except ValidationError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e),
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
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=exc.detail,
            request_id=request.headers.get("x-rh-insights-request-id"),
        ).dict(),
    )


async def _watch_certs(application: FastAPI):
    """Watch TLS cert directories and reload SSLContext when they change.

    Kubernetes Secret volumes use symlinks (..data → timestamped dir).
    Watching the directories catches the symlink swap that inotify on
    individual files would miss.
    """
    async for _ in awatch(TLS_DIR):
        try:
            application.state.ssl_context.load_cert_chain(TLS_CERT, TLS_KEY)
            application.state.ssl_context.load_verify_locations(cafile=CLIENT_CA_PATH)
            application.state.cert_reload_error = None
            logger.info("Reloaded SSLContext with updated certificates")
        except Exception:
            application.state.cert_reload_error = (
                "Failed to reload certificates — serving stale certs"
            )
            logger.exception(application.state.cert_reload_error)


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
