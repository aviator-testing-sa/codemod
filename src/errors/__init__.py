#
# List of exceptions

class ReinventError(Exception):
    pass

class ValidationError(ReinventError):
    pass


class InvalidValueError(ReinventError):
    pass


class InvalidRequestError(ReinventError):
    pass

class AccessDeniedError(ReinventError):
    pass
