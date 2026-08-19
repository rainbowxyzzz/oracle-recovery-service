class RecoveryServiceError(Exception):
    """Base exception."""


class DiscoveryError(RecoveryServiceError):
    pass


class PolicyTreeError(RecoveryServiceError):
    pass


class ImpdpError(RecoveryServiceError):
    def __init__(self, message: str, stderr: str = "", return_code: int = 1):
        super().__init__(message)
        self.stderr = stderr
        self.return_code = return_code


class OraCorrectionExhaustedError(RecoveryServiceError):
    pass


class RemoteAccessError(RecoveryServiceError):
    pass


class TargetDatabaseConnectionError(RecoveryServiceError):
    pass
