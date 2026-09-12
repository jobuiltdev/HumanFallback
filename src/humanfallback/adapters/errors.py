"""Error codes shared by every adapter so callers handle them uniformly."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    # Local or API validation
    AMOUNT_OUT_OF_RANGE = "AMOUNT_OUT_OF_RANGE"
    UNSUPPORTED_MINT = "UNSUPPORTED_MINT"
    MISSING_TOKEN_ACCOUNT = "MISSING_TOKEN_ACCOUNT"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    NOT_FOUND = "NOT_FOUND"
    # Prepared-confirmation lifecycle
    CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"
    CONFIRMATION_INVALID = "CONFIRMATION_INVALID"
    # Environment
    CREDENTIAL_ERROR = "CREDENTIAL_ERROR"
    CONFIG_ERROR = "CONFIG_ERROR"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    # Transport
    NETWORK_ERROR = "NETWORK_ERROR"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    AMBIGUOUS_SUBMIT = "AMBIGUOUS_SUBMIT"
    # Anything else the backend reported
    API_ERROR = "API_ERROR"


class AdapterError(Exception):
    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = ErrorCode(code) if isinstance(code, str) else code
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class AmbiguousSubmit(AdapterError):
    """A money-moving submit was dispatched but its outcome is unknown.

    Never retry. Reconcile with the recorded identifiers instead.
    """

    def __init__(
        self,
        message: str,
        *,
        operation: str,
        confirmation_id: str,
        task_id: str | None = None,
        intent_id: str | None = None,
        cause: str | None = None,
    ) -> None:
        super().__init__(
            ErrorCode.AMBIGUOUS_SUBMIT,
            message,
            details={
                "operation": operation,
                "confirmationId": confirmation_id,
                "taskId": task_id,
                "intentId": intent_id,
                "cause": cause,
            },
        )
        self.operation = operation
        self.confirmation_id = confirmation_id
        self.task_id = task_id
        self.intent_id = intent_id
        self.cause = cause
