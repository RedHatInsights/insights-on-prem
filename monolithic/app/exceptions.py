"""Custom exceptions for the application."""


class ValidationError(Exception):
    """Raised when validation fails."""
    pass


class ProcessingError(Exception):
    """Raised when archive processing fails."""
    pass


class NotFoundError(Exception):
    """Raised when a resource is not found."""
    pass
