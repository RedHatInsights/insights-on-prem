"""Service for upload orchestration and validation."""
import logging
import os
import tempfile
from datetime import datetime
from typing import Tuple

from fastapi import BackgroundTasks, UploadFile
from sqlalchemy.orm import sessionmaker

from app.config import AppConfig
from app.schemas import UploadResponse
from app.services.processor_service import ProcessorService
from app.exceptions import ValidationError

logger = logging.getLogger(__name__)


class UploadService:
    """Service for handling archive uploads and processing orchestration."""

    def __init__(
        self,
        processor_service: ProcessorService,
        config: AppConfig,
        session_factory: sessionmaker,
    ):
        """
        Initialize the upload service.

        :param processor_service: Processor service instance
        :param config: Application configuration
        :param session_factory: SQLAlchemy session factory for background tasks
        """
        self.processor_service = processor_service
        self.config = config
        self.session_factory = session_factory

    def _get_archive_suffix(self, file: UploadFile) -> str:
        suffix = ""
        if file.filename.endswith('.tar.gz'):
            suffix = '.tar.gz'
        elif file.filename.endswith('.tgz'):
            suffix = '.tgz'
        elif file.filename.endswith('.tar'):
            suffix = '.tar'
        return suffix

    def _validate_file(self, file: UploadFile, request_id: str) -> None:
        """
        Validate uploaded file.

        :param file: Uploaded file
        :param request_id: Request ID for logging
        :raises ValidationError: If validation fails
        """
        if not file.filename:
            logger.warning(f"Request {request_id}: No filename provided")
            raise ValidationError("No filename provided")

        if self._get_archive_suffix(file) == "":
            logger.warning(f"Request {request_id}: Invalid file format: {file.filename}")
            raise ValidationError("File must be a .tar, .tar.gz, or .tgz archive")

    async def _save_to_temp(
        self, file: UploadFile, request_id: str
    ) -> Tuple[str, int]:
        """
        Save uploaded file to temporary location.

        :param file: Uploaded file
        :param request_id: Request ID for logging
        :return: Tuple of (temp_file_path, total_size)
        :raises ValidationError: If file size exceeds limit
        """
        # Create temporary file
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=self._get_archive_suffix(file),
            dir=self.config.temp_upload_dir,
        ) as temp_file:
            temp_file_path = temp_file.name

            # Read and validate file size
            chunk_size = 1024 * 1024  # 1MB chunks
            total_size = 0

            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > self.config.max_file_size:
                    # Clean up temp file before raising
                    try:
                        os.remove(temp_file_path)
                    except:
                        pass

                    logger.warning(
                        f"Request {request_id}: File too large ({total_size} bytes)"
                    )
                    raise ValidationError(
                        f"File size exceeds maximum allowed size of {self.config.max_file_size} bytes"
                    )

                temp_file.write(chunk)

        logger.info(
            f"Request {request_id}: Saved uploaded file ({total_size} bytes) to {temp_file_path}"
        )

        return temp_file_path, total_size

    def _process_in_background(self, temp_file_path: str, request_id: str) -> None:
        """
        Process archive in a background task.

        :param temp_file_path: Path to temporary archive file
        :param request_id: Request ID for logging
        """
        try:
            db = self.session_factory()
            try:
                cluster_id, rules_count = self.processor_service.process_archive(
                    db, temp_file_path
                )
                logger.info(
                    f"Request {request_id}: Successfully processed cluster {cluster_id} "
                    f"with {rules_count} rules"
                )
            except Exception as e:
                logger.error(f"Request {request_id}: Background processing failed: {e}", exc_info=True)
            finally:
                db.close()
        finally:
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                    logger.debug(f"Cleaned up temporary file: {temp_file_path}")
                except Exception as e:
                    logger.warning(f"Failed to clean up temporary file: {e}")

    async def process_upload(
        self, background_tasks: BackgroundTasks, file: UploadFile, request_id: str
    ) -> UploadResponse:
        """
        Validate and save upload, then schedule processing as a background task.

        :param background_tasks: FastAPI BackgroundTasks
        :param file: Uploaded file
        :param request_id: Request ID
        :return: UploadResponse with accepted status
        :raises ValidationError: On validation errors
        """
        logger.info(f"Upload request {request_id}")

        # Validate file
        self._validate_file(file, request_id)

        # Save to temp location
        temp_file_path, total_size = await self._save_to_temp(file, request_id)

        # Schedule processing as background task
        background_tasks.add_task(self._process_in_background, temp_file_path, request_id)

        return UploadResponse(
            request_id=request_id,
            status="accepted",
            uploaded_at=datetime.utcnow(),
        )
