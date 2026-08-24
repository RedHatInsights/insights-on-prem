"""Custom exceptions for the application."""


class AppError(Exception):
    """Base exception for all application errors."""

    pass


class ValidationError(AppError):
    """Raised when validation fails."""

    pass


class ProcessingError(AppError):
    """Raised when archive processing fails."""

    pass


class ProcessorBusyError(AppError):
    """Raised when the archive processor queue is full."""

    pass


class NotFoundError(AppError):
    """Raised when a resource is not found."""

    pass
