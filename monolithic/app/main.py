"""FastAPI application for Insights On Premise."""
import logging
import os
import uuid

from fastapi import FastAPI, File, UploadFile, Depends, HTTPException, Header
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import init_db, get_db
from app.schemas import (
    UploadResponse,
    ErrorResponse,
    ReportResponseV2,
)
from app.dependencies import get_content_service, get_report_service, get_upload_service
from app.services.report_service import ReportService
from app.services.upload_service import UploadService
from app.exceptions import ValidationError, ProcessingError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()

# Create FastAPI app
app = FastAPI(
    title="Insights On-Premise",
    description="Red Hat Insights archive processing for on-premise deployment",
    version="1.0.0",
)


@app.on_event("startup")
async def startup_event():
    """Initialize application on startup."""
    logger.info("Starting Insights On-Premise application")

    # Ensure temp upload directory exists
    os.makedirs(settings.temp_upload_dir, exist_ok=True)
    logger.info(f"Temp upload directory: {settings.temp_upload_dir}")

    # Initialize database
    try:
        init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")

    # Initialize content service (loads YAML/markdown files into memory, like content-service)
    try:
        content_service = get_content_service()
        logger.info("Content service initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize content service: {e}", exc_info=True)


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
    f"{settings.api_prefix}/upload",
    response_model=UploadResponse,
    status_code=202,
    responses={
        400: {"model": ErrorResponse, "description": "Bad Request"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Internal Server Error"},
    },
)
async def upload_archive(
    file: UploadFile = File(...),
    x_rh_insights_request_id: str = Header(None, alias="x-rh-insights-request-id"),
    db: Session = Depends(get_db),
    upload_service: UploadService = Depends(get_upload_service),
):
    """
    Upload and process Red Hat Insights archive.

    Args:
        file: Uploaded archive file (tar, tar.gz, or tgz format)
        x_rh_insights_request_id: Optional request ID header
        db: Database session
        upload_service: Upload service instance

    Returns:
        UploadResponse with processing results

    Raises:
        HTTPException: On validation or processing errors
    """
    # Generate or use provided request ID
    request_id = x_rh_insights_request_id or str(uuid.uuid4())

    try:
        return await upload_service.process_upload(db, file, request_id)

    except ValidationError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e),
        )

    except ProcessingError as e:
        logger.error(f"Request {request_id}: Processing error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Archive processing failed: {str(e)}",
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
    cluster_id: str,
    get_disabled: bool = False,
    db: Session = Depends(get_db),
    report_service: ReportService = Depends(get_report_service),
):
    """
    Retrieve the latest report for a specific cluster (v2 endpoint).

    This endpoint returns the latest report for the given cluster ID,
    following the v2 API format used by insights-results-smart-proxy.

    Args:
        cluster_id: Cluster UUID
        get_disabled: If true, disabled rules will be included
        db: Database session
        report_service: Report service instance

    Returns:
        ReportResponseV2 with detailed report data

    Raises:
        HTTPException: On not found or processing errors
    """
    try:
        report_v2 = report_service.get_cluster_report_v2(db, cluster_id, get_disabled)
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


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    """Custom handler for HTTP exceptions."""
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
        log_level=settings.log_level.lower(),
    )
