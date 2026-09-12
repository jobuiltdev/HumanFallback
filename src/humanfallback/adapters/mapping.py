"""Translate Gibwork MCP tool payloads into HumanFallback domain models.

Shapes follow what the stage server returns today. Mapping is lenient about
optional fields and keeps the original payload on models whose full shape
has not been observed yet, so nothing is hidden from the person approving
a payment.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from humanfallback.models import (
    PaymentQuote,
    PlatformFee,
    PrepareResult,
    RefundPrepareResult,
    RemoteSubmission,
    RemoteTask,
    SubmitResult,
    TokenInfo,
    WalletStatus,
)

from .errors import AdapterError, ErrorCode

_CENTS = Decimal("0.01")


def two_decimals(value: Decimal | str | int | float) -> str:
    return str(Decimal(str(value)).quantize(_CENTS))


def base_units_to_amount(raw: str | int, decimals: int) -> str:
    """'10000000' with 6 decimals -> '10.00'."""
    return two_decimals(Decimal(str(raw)) / (Decimal(10) ** decimals))


def parse_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _get(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return default


# -- wallet -------------------------------------------------------------------


def map_wallet_status(payload: dict[str, Any], *, adapter: str) -> WalletStatus:
    return WalletStatus(
        adapter=adapter,
        profile=str(_get(payload, "profile", default="")),
        environment=str(_get(payload, "environment", default="")),
        wallet_address=str(_get(payload, "walletAddress", "wallet_address", default="")),
        writes_enabled=bool(_get(payload, "writesEnabled", "writes_enabled", default=False)),
    )


# -- tokens and quotes ----------------------------------------------------------


def map_token(payload: dict[str, Any]) -> TokenInfo:
    return TokenInfo(
        mint_address=str(_get(payload, "mintAddress", "mint_address", default="")),
        symbol=str(_get(payload, "symbol", default="")),
        decimals=int(_get(payload, "decimals", default=0)),
    )


def map_payment_quote(payload: dict[str, Any]) -> PaymentQuote:
    fee = payload.get("platformFee") or {}
    return PaymentQuote(
        token=map_token(payload.get("token") or {}),
        funding_amount=two_decimals(_get(payload, "fundingAmount", default="0")),
        platform_fee=PlatformFee(
            percent=float(_get(fee, "percent", default=0)),
            amount=two_decimals(_get(fee, "amount", default="0")),
        ),
        total_debit=two_decimals(_get(payload, "totalDebit", default="0")),
    )


# -- bounty creation ----------------------------------------------------------


def map_prepare_result(payload: dict[str, Any]) -> PrepareResult:
    created = parse_datetime(payload.get("createdAt"))
    expires = parse_datetime(payload.get("expiresAt"))
    if created is None or expires is None:
        raise AdapterError(ErrorCode.PROTOCOL_ERROR, "prepare response lacks timestamps")
    block = payload.get("lastValidBlockHeight")
    return PrepareResult(
        confirmation_id=str(payload["confirmationId"]),
        intent_id=str(payload["intentId"]),
        task_id=str(payload["taskId"]),
        wallet_address=str(_get(payload, "walletAddress", default="")),
        environment=str(_get(payload, "environment", default="")),
        created_at=created,
        expires_at=expires,
        payment_quote=map_payment_quote(payload.get("paymentQuote") or {}),
        next_action=str(_get(payload, "nextAction", default="")),
        last_valid_block_height=int(block) if block is not None else None,
    )


def map_submit_result(
    payload: dict[str, Any],
    *,
    confirmation_id: str,
    task_id: str | None,
    intent_id: str | None,
) -> SubmitResult:
    """The submit payload shape has not been observed on stage; read what is
    there and fall back to the identifiers recorded at prepare time."""
    signature = _get(
        payload, "signature", "transactionSignature", "txSignature", "txid", default=""
    )
    resolved_task = _get(payload, "taskId", "task_id", default=task_id)
    if not resolved_task:
        raise AdapterError(ErrorCode.PROTOCOL_ERROR, "submit response lacks a task id")
    submitted = parse_datetime(_get(payload, "submittedAt", "createdAt")) or datetime.now(UTC)
    return SubmitResult(
        task_id=str(resolved_task),
        intent_id=_get(payload, "intentId", "intent_id", default=intent_id),
        confirmation_id=confirmation_id,
        signature=str(signature),
        status=str(_get(payload, "status", default="submitted")),
        submitted_at=submitted,
        raw=dict(payload),
    )


# -- refund -------------------------------------------------------------------


def map_refund_prepare(payload: dict[str, Any], *, task_id: str) -> RefundPrepareResult:
    quote = payload.get("paymentQuote") or payload.get("refundQuote") or {}
    token_payload = quote.get("token") or payload.get("token") or payload.get("asset")
    amount = _get(quote, "refundAmount", "amount", "totalCredit", "fundingAmount")
    if amount is None:
        amount = _get(payload, "refundAmount", "amount")
    return RefundPrepareResult(
        confirmation_id=str(payload["confirmationId"]),
        task_id=str(_get(payload, "taskId", default=task_id)),
        intent_id=_get(payload, "intentId"),
        wallet_address=_get(payload, "walletAddress"),
        environment=_get(payload, "environment"),
        created_at=parse_datetime(payload.get("createdAt")),
        expires_at=parse_datetime(payload.get("expiresAt")),
        refund_amount=two_decimals(amount) if amount is not None else None,
        token=map_token(token_payload) if isinstance(token_payload, dict) else None,
        next_action=_get(payload, "nextAction"),
        raw=dict(payload),
    )


# -- tasks --------------------------------------------------------------------


def map_remote_task(payload: dict[str, Any]) -> RemoteTask:
    asset = payload.get("asset") or {}
    token = map_token(asset)
    raw_amount = _get(asset, "amount", default="0")
    funding = base_units_to_amount(raw_amount, token.decimals) if token.decimals else two_decimals(raw_amount)
    # minSubmissionAmount is observed in token units ("1" for 1.00 USDC).
    min_sub = two_decimals(_get(payload, "minSubmissionAmount", default="0"))
    status = str(_get(payload, "status", default=""))
    is_open = payload.get("isOpen")
    if is_open is None:
        is_open = status.upper() == "CREATED"
    total = payload.get("totalSubmissions")
    max_sub = payload.get("maxSubmissions")
    return RemoteTask(
        id=str(payload["id"]),
        title=str(_get(payload, "title", default="")),
        content=str(_get(payload, "content", default="")),
        tags=[str(t) for t in payload.get("tags") or []],
        status=status,
        is_open=bool(is_open),
        token=token,
        funding_amount=funding,
        min_submission_amount=min_sub,
        deadline=parse_datetime(payload.get("deadline")),
        created_at=parse_datetime(payload.get("createdAt")) or datetime.now(UTC),
        total_submissions=int(total) if total is not None else None,
        max_submissions=int(max_sub) if max_sub is not None else None,
        can_refund=payload.get("canRefund"),
    )


# -- submissions --------------------------------------------------------------


def map_submission(payload: dict[str, Any], *, task_id: str) -> RemoteSubmission:
    submitter = payload.get("user") or payload.get("submitter") or payload.get("contributor")
    if isinstance(submitter, dict):
        submitter = _get(submitter, "username", "walletAddress", "wallet", "id")
    media = payload.get("media") or []
    media_urls = [
        str(m.get("url") or m.get("id")) if isinstance(m, dict) else str(m) for m in media
    ]
    rating = payload.get("rating")
    return RemoteSubmission(
        id=str(payload["id"]),
        task_id=str(_get(payload, "taskId", default=task_id)),
        status=str(_get(payload, "status", "reviewStatus", default="pending")).lower(),
        content=str(_get(payload, "content", default="")),
        submitter=str(submitter) if submitter else None,
        media=media_urls,
        rating=int(rating) if rating is not None else None,
        created_at=parse_datetime(payload.get("createdAt")),
        comments=list(payload.get("comments") or []),
        raw=dict(payload),
    )


def unwrap_list(payload: Any) -> list[dict[str, Any]]:
    """Paginated tools return {"results": [...], ...}; accept a bare list too."""
    if isinstance(payload, dict):
        items = payload.get("results") or payload.get("items") or payload.get("data") or []
    else:
        items = payload or []
    return [i for i in items if isinstance(i, dict)]


# -- errors -------------------------------------------------------------------


def translate_error(payload: Any) -> AdapterError:
    """Map the MCP tool error body to a stable AdapterError code."""
    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):
        return AdapterError(ErrorCode.API_ERROR, str(payload)[:500])
    code = str(err.get("code") or "API_ERROR")
    message = str(err.get("message") or "")
    details = err.get("details") if isinstance(err.get("details"), dict) else {}
    lower = message.lower()

    if code == "PENDING_INTENT_ERROR":
        # "has expired" / "expired before" are definite; "unknown, expired, or
        # already used" is not, and must not be treated as safely re-preparable.
        if "has expired" in lower or "expired before" in lower:
            return AdapterError(ErrorCode.CONFIRMATION_EXPIRED, message, details=details)
        return AdapterError(ErrorCode.CONFIRMATION_INVALID, message, details=details)
    if code in ("CREDENTIAL_ERROR",):
        return AdapterError(ErrorCode.CREDENTIAL_ERROR, message, details=details)
    if code in ("CONFIG_ERROR", "USAGE_ERROR"):
        return AdapterError(ErrorCode.CONFIG_ERROR, message, details=details)
    if code in ("NETWORK_ERROR", "TIMEOUT_ERROR"):
        return AdapterError(ErrorCode.NETWORK_ERROR, message, details=details)

    if "token account" in lower:
        return AdapterError(ErrorCode.MISSING_TOKEN_ACCOUNT, message, details=details)
    if "must be between" in lower and "amount" in lower:
        return AdapterError(ErrorCode.AMOUNT_OUT_OF_RANGE, message, details=details)
    if "insufficient" in lower:
        return AdapterError(ErrorCode.INSUFFICIENT_FUNDS, message, details=details)
    if "mint" in lower and ("unsupported" in lower or "not supported" in lower or "invalid" in lower):
        return AdapterError(ErrorCode.UNSUPPORTED_MINT, message, details=details)
    if details.get("status") == 404 or "not found" in lower:
        return AdapterError(ErrorCode.NOT_FOUND, message, details=details)
    return AdapterError(ErrorCode.API_ERROR, message or code, details=details)
