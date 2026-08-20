"""Service for upload orchestration and validation."""

import contextlib
import logging
import os
import tempfile
from datetime import datetime, timezone
from multiprocessing.queues import Queue

from fastapi import UploadFile

from app.config import AppConfig
from app.exceptions import ValidationError
from app.schemas import UploadResponse

logger = logging.getLogger(__name__)


class UploadService:
    """Service for handling archive uploads and processing orchestration."""

    def __init__(self, config: AppConfig, archive_queue: Queue):
        self.config = config
        self.archive_queue = archive_queue

    def _get_archive_suffix(self, file: UploadFile) -> str:
        suffix = ""
        if file.filename.endswith(".tar.gz"):
            suffix = ".tar.gz"
        elif file.filename.endswith(".tgz"):
            suffix = ".tgz"
        elif file.filename.endswith(".tar"):
            suffix = ".tar"
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
            logger.warning(
                f"Request {request_id}: Invalid file format: {file.filename}"
            )
            raise ValidationError("File must be a .tar, .tar.gz, or .tgz archive")

    async def _save_to_temp(self, file: UploadFile, request_id: str) -> tuple[str, int]:
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
                    with contextlib.suppress(Exception):
                        os.remove(temp_file_path)

                    logger.warning(
                        f"Request {request_id}: File too large ({total_size} bytes)"
                    )
                    raise ValidationError(
                        f"File size exceeds maximum allowed size of "
                        f"{self.config.max_file_size} bytes"
                    )

                temp_file.write(chunk)

        logger.info(
            f"Request {request_id}: Saved uploaded file ({total_size} bytes) to {temp_file_path}"
        )

        return temp_file_path, total_size

    async def process_upload(self, file: UploadFile, request_id: str) -> UploadResponse:
        """
        Validate and save upload, then enqueue it for processing.

        :param file: Uploaded file
        :param request_id: Request ID
        :return: UploadResponse with accepted status
        :raises ValidationError: On validation errors
        """
        logger.info(f"Upload request {request_id}")

        self._validate_file(file, request_id)
        temp_file_path, _ = await self._save_to_temp(file, request_id)
        self.archive_queue.put((temp_file_path, request_id))

        return UploadResponse(
            request_id=request_id,
            status="accepted",
            uploaded_at=datetime.now(timezone.utc),
        )
