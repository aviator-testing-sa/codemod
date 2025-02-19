# List of exceptions

class ReinventError(Exception):
    """Base class for ReInvent exceptions."""
    pass

class ValidationError(ReinventError):
    """Raised when input data fails validation."""
    pass


class InvalidValueError(ReinventError):
    """Raised when a value is invalid."""
    pass


class InvalidRequestError(ReinventError):
    """Raised when a request is invalid."""
    pass

class AccessDeniedError(ReinventError):
    """Raised when access is denied."""
    pass
