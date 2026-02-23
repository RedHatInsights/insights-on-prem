"""FastAPI application for Insights On Premise."""
import logging
import os
import uuid

from contextlib import asynccontextmanager
from fastapi import BackgroundTasks, FastAPI, File, Request, UploadFile, Depends, HTTPException, Header
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config_loader import load_config, load_insights_components
from app.database import init_db, get_db
from app.schemas import (
    UploadResponse,
    ErrorResponse,
    ReportResponseV2,
    UpgradeRisksPredictionRequest,
    UpgradeRisksPredictionResponse,
)
from app.content_parser_yaml import YAMLContentParser
from app.services.report_service import ReportService
from app.services.upload_service import UploadService
from app.services.processor_service import ProcessorService
from app.services.content_service import ContentService
from app.services.thanos_service import ThanosService
from app.services.upgrade_prediction_service import UpgradePredictionService
from app.exceptions import ValidationError

logger = logging.getLogger(__name__)


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
    app.state.upload_service = UploadService(app.state.processor_service, config, session_factory)
    app.state.content_service = ContentService(YAMLContentParser())
    app.state.report_service = ReportService(app.state.content_service)
    app.state.thanos_service = ThanosService(config)
    app.state.upgrade_prediction_service = UpgradePredictionService()
    logger.info("All services initialized successfully")

    yield


# Create FastAPI app
app = FastAPI(
    title="Insights On-Premise",
    description="Red Hat Insights archive processing for on-premise deployment",
    version="1.0.0",
    lifespan=lifespan
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
    },
)
async def upload_archive(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    x_rh_insights_request_id: str = Header(None, alias="x-rh-insights-request-id"),
):
    """
    Upload and process Red Hat Insights archive.

    :param file: Uploaded archive file (tar, tar.gz, or tgz format)
    :param background_tasks: FastAPI background tasks
    :param x_rh_insights_request_id: Optional request ID header
    :return: UploadResponse with accepted status
    :raises HTTPException: On validation errors
    """
    upload_service: UploadService = request.app.state.upload_service

    # Generate or use provided request ID
    request_id = x_rh_insights_request_id or str(uuid.uuid4())

    try:
        return await upload_service.process_upload(background_tasks, file, request_id)

    except ValidationError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e),
        )

    except Exception as e:
        logger.error(f"Request {request_id}: Unexpected error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Internal server error during upload processing",
        )


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
    db: Session = Depends(get_db),
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
        )

    except Exception as e:
        logger.error(
            f"Error fetching v2 report for cluster {cluster_id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error while fetching cluster report",
        )


@app.post(
    "/upgrade-risks-prediction",
    response_model=UpgradeRisksPredictionResponse,
    status_code=200,
    responses={
        500: {"model": ErrorResponse, "description": "Internal Server Error"},
    },
)
async def upgrade_risks_prediction(
    request: Request,
    body: UpgradeRisksPredictionRequest,
):
    """
    Predict upgrade risks for a cluster based on current alerts and operator conditions.

    Queries Thanos for active alerts and failing operator conditions,
    then applies static filtering rules to identify actual upgrade risks.

    :param body: Request body containing cluster_id
    :return: UpgradeRisksPredictionResponse with recommendation and risks
    """
    thanos_service: ThanosService = request.app.state.thanos_service
    prediction_service: UpgradePredictionService = request.app.state.upgrade_prediction_service

    try:
        console_url, alerts, focs = thanos_service.query_cluster_metrics(body.cluster_id)
        return prediction_service.predict(alerts, focs, console_url)

    except Exception as e:
        logger.error(
            f"Error predicting upgrade risks for cluster {body.cluster_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error while predicting upgrade risks",
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
