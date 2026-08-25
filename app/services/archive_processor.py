"""Single-thread worker that processes uploaded archives from a queue."""

import logging
import os
import time
from queue import Full, Queue
from threading import Thread

from sqlalchemy.orm import sessionmaker

from app.services.processor_service import ProcessorService

logger = logging.getLogger(__name__)

_STOP = None
DEFAULT_QUEUE_SIZE = 10


class ArchiveProcessor(Thread):
    """Consume archive jobs from a bounded queue on a single thread."""

    def __init__(
        self,
        processor_service: ProcessorService,
        session_factory: sessionmaker,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        queue: Queue | None = None,
    ):
        super().__init__(name="archive-processor", daemon=True)
        self.processor_service = processor_service
        self.session_factory = session_factory
        if queue is not None:
            self.queue = queue
        else:
            if queue_size < 1:
                raise ValueError("queue_size must be at least 1")
            self.queue: Queue = Queue(maxsize=queue_size)
        self._stop_requested = False

    def run(self) -> None:
        logger.info(
            "Archive processor thread started (queue maxsize=%s)",
            self.queue.maxsize,
        )
        while True:
            job = self.queue.get()
            if job is _STOP:
                logger.info("Archive processor thread stopping")
                break
            try:
                temp_file_path, request_id = job
                self._process_job(temp_file_path, request_id)
            except Exception as e:
                logger.error(f"Archive processor: unexpected error: {e}", exc_info=True)
        logger.info("Archive processor thread stopped")

    def _process_job(self, temp_file_path: str, request_id: str) -> None:
        try:
            db = self.session_factory()
            try:
                cluster_id, rules_count = self.processor_service.process_archive(
                    db, temp_file_path, request_id
                )
                logger.info(
                    f"Request {request_id}: Successfully processed cluster {cluster_id} "
                    f"with {rules_count} rules"
                )
            except Exception as e:
                logger.error(
                    f"Request {request_id}: Archive processing failed: {e}",
                    exc_info=True,
                )
            finally:
                db.close()
        finally:
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                    logger.debug(f"Cleaned up temporary file: {temp_file_path}")
                except Exception as e:
                    logger.warning(f"Failed to clean up temporary file: {e}")

    def stop(self, timeout: float | None = None) -> bool:
        """Enqueue a stop sentinel and wait for the worker thread to exit.

        :param timeout: Seconds to wait for the thread to finish
        :return: True if the thread stopped within the timeout
        """
        if self.ident is None:
            return True

        deadline = None if timeout is None else time.monotonic() + timeout

        def remaining() -> float | None:
            if deadline is None:
                return None
            return max(0.0, deadline - time.monotonic())

        if not self._stop_requested:
            try:
                self.queue.put(_STOP, timeout=remaining())
            except Full:
                logger.warning(
                    "Queue is full; stop sentinel was not enqueued before timeout"
                )
            else:
                self._stop_requested = True
        self.join(remaining())
        return not self.is_alive()
