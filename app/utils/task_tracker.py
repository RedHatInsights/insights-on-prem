"""Thread-safe tracker for in-flight background tasks."""

import threading


class BackgroundTaskTracker:
    """Thread-safe tracker for in-flight background tasks."""

    def __init__(self):
        self._lock = threading.Lock()
        self._count = 0
        self._idle = threading.Event()
        self._idle.set()

    def start(self):
        with self._lock:
            self._count += 1
            self._idle.clear()

    def finish(self):
        with self._lock:
            self._count -= 1
            if self._count == 0:
                self._idle.set()

    def wait_until_idle(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout=timeout)

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._count
