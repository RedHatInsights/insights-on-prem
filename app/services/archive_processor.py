"""Single-thread worker that processes uploaded archives from a queue."""

import logging
import os
from queue import Empty, Full, Queue
from threading import Lock, Thread

from sqlalchemy.orm import sessionmaker

from app.services.processor_service import ProcessorService

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 10
_IDLE_GET_TIMEOUT = 0.2


class ArchiveQueue:
    """Bounded job queue that can be closed to reject further submissions."""

    def __init__(self, maxsize: int):
        self._queue: Queue = Queue(maxsize=maxsize)
        self._lock = Lock()
        self._closed = False

    @property
    def maxsize(self) -> int:
        return self._queue.maxsize

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def put_nowait(self, item) -> None:
        with self._lock:
            if self._closed:
                raise Full
            self._queue.put_nowait(item)

    def get(self, timeout: float | None = None):
        return self._queue.get(timeout=timeout)

    def empty(self) -> bool:
        return self._queue.empty()


class ArchiveProcessor(Thread):
    """Consume archive jobs from a bounded queue on a single thread."""

    def __init__(
        self,
        processor_service: ProcessorService,
        session_factory: sessionmaker,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ):
        super().__init__(name="archive-processor", daemon=True)
        self.processor_service = processor_service
        self.session_factory = session_factory
        self.queue = ArchiveQueue(maxsize=queue_size)

    def run(self) -> None:
        logger.info(
            "Archive processor thread started (queue maxsize=%s)",
            self.queue.maxsize,
        )
        while True:
            try:
                job = self.queue.get(timeout=_IDLE_GET_TIMEOUT)
            except Empty:
                if self.queue.closed:
                    break
                continue
            try:
                temp_file_path, request_id = job
                self._process_job(temp_file_path, request_id)
            except Exception as e:
                logger.error(f"Archive processor: unexpected error: {e}", exc_info=True)
            if self.queue.closed and self.queue.empty():
                break
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
        """Stop accepting new jobs, drain the queue, and join the worker thread.

        :param timeout: Seconds to wait for the thread to finish
        :return: True if the thread stopped within the timeout
        """
        self.queue.close()
        if self.ident is None:
            return True
        self.join(timeout)
        return not self.is_alive()
