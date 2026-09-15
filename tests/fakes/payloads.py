"""Payloads captured from the Gibwork stage MCP server, used as fixtures.

Identifiers are real but refer to a prepare that was never submitted and
has long since expired.
"""

from __future__ import annotations

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLET = "G27rcziyduXcdG6rFt7g96FijjkWgYiDBYeviBqJj6Kw"

WALLET_STATUS = {
    "profile": "default",
    "environment": "stage",
    "walletAddress": WALLET,
    "writesEnabled": True,
    "timeoutMs": 60000,
}

WALLET_STATUS_READ_ONLY = {**WALLET_STATUS, "writesEnabled": False}

PREPARE_RESULT = {
    "confirmationId": "fa30e95b-69bb-4db5-888d-692848b15ea4",
    "intentId": "517a18d2-50be-4a49-b691-c40077615cad",
    "taskId": "6fe2affc-97fe-4943-a4ca-7604b545c0e0",
    "walletAddress": WALLET,
    "profile": "default",
    "environment": "stage",
    "createdAt": "2026-09-11T20:48:06.643Z",
    "expiresAt": "2026-09-11T20:53:06.643Z",
    "lastValidBlockHeight": 424294625,
    "paymentQuote": {
        "token": {"mintAddress": USDC, "symbol": "USDC", "decimals": 6},
        "fundingAmount": "1.00",
        "platformFee": {"percent": 0, "amount": "0.00"},
        "totalDebit": "1.00",
    },
    "nextAction": (
        "Review the quote, then request human approval before calling "
        "gibwork_task_create_submit with this confirmationId."
    ),
}

TASK_ITEM = {
    "id": "1207de60-cde8-46b9-b5b7-9fb75b2e71b6",
    "slug": "create-a-tokendollarhit-meme-fihyluct",
    "title": "Create a TOKEN$HIT meme",
    "content": "<p>Make a meme, tweet it, and tag the account.</p>",
    "requirements": None,
    "tags": ["Social Media", "Design"],
    "primarySkillId": "1cf10ba1-bec5-49a6-b359-01ac00e73c8e",
    "createdAt": "2026-08-29T22:43:14.470Z",
    "deadline": None,
    "status": "CREATED",
    "isOpen": True,
    "asset": {
        "mintAddress": USDC,
        "symbol": "USDC",
        "imageUrl": "https://cdn.gib.work/token-images/usdc.png",
        "decimals": 6,
        "amount": "10000000",
    },
    "minSubmissionAmount": "1",
    "totalSubmissions": 1,
    "maxSubmissions": 10,
    "standardSubmissionSlotsRemaining": 9,
    "requiresPremium": False,
    "participationRequirements": {
        "allowOnlyVerifiedSubmissions": False,
        "allowOnlyDiscordGuildSubmissions": False,
        "requiredDiscordGuildId": None,
        "requiredDiscordRoleIds": [],
    },
}

TASK_LIST = {"results": [TASK_ITEM], "page": 1, "limit": 50, "total": 1, "lastPage": 1}

# gibwork_task_get (CLI 0.2.4 / MCP 0.3.1): no totalSubmissions, per-status
# counts instead, minSubmissionAmount as a number, no canRefund.
TASK_GET_ITEM = {
    "id": TASK_ITEM["id"],
    "title": TASK_ITEM["title"],
    "content": TASK_ITEM["content"],
    "requirements": "",
    "tags": ["Social Media", "Design"],
    "primarySkill": {"slug": "other", "label": "Other"},
    "isFeatured": False,
    "createdAt": "2026-08-29T22:43:14.470Z",
    "isOpen": True,
    "slug": TASK_ITEM["slug"],
    "refundTransactionId": None,
    "allowOnlyVerifiedSubmissions": False,
    "maxSubmissions": 10,
    "deadline": None,
    "taskSubmissionsApprovedCount": 1,
    "taskSubmissionsPendingCount": 2,
    "taskSubmissionsRejectedCount": 0,
    "minSubmissionAmount": 1,
    "status": "CREATED",
    "media": [],
    "asset": {
        "id": "da683d02-0c05-4bb1-be7a-0af1b284a075",
        "mintAddress": USDC,
        "symbol": "USDC",
        "amount": "10000000",
        "price": 1,
        "decimals": 6,
    },
    "hasSubmission": False,
    "userSubmissionStatus": None,
}
TASK_LIST_EMPTY = {"results": [], "page": 1, "limit": 50, "total": 0, "lastPage": 0}

SUBMISSION_ITEM = {
    "id": "0a3e0b1c-1111-4222-8333-444455556666",
    "taskId": TASK_ITEM["id"],
    "status": "pending",
    "content": "<p>Here is my meme: https://x.com/example/status/1</p>",
    "user": {"username": "meme_worker", "walletAddress": "Worker111111111111111111111111111111111111"},
    "media": [{"id": "m1", "url": "https://cdn.gib.work/media/m1.png"}],
    "rating": None,
    "createdAt": "2026-09-01T10:00:00.000Z",
    "comments": [],
}

SUBMISSION_LIST = {"results": [SUBMISSION_ITEM], "page": 1, "limit": 15, "total": 1, "lastPage": 1}

ERROR_AMOUNT_RANGE = {
    "error": {
        "code": "API_ERROR",
        "message": (
            "payment.payment.amount must be between 1.00 and 100000.00 inclusive; "
            "payment.amount must be between 1.00 and 100000.00 inclusive"
        ),
        "details": {
            "status": 400,
            "method": "POST",
            "path": "/v2/int/tasks",
            "requestId": "20597f90-02e0-4202-b4e4-8029b0343a57",
            "body": {
                "message": [
                    "payment.payment.amount must be between 1.00 and 100000.00 inclusive",
                    "payment.amount must be between 1.00 and 100000.00 inclusive",
                ],
                "error": "Bad Request",
                "statusCode": 400,
            },
        },
    }
}

ERROR_TOKEN_ACCOUNT = {
    "error": {
        "code": "API_ERROR",
        "message": (
            "The funding wallet does not have an initialized USDC token account. "
            "Deposit USDC and try again."
        ),
        "details": {
            "status": 400,
            "method": "POST",
            "path": "/v2/int/tasks",
            "requestId": "c1dd8b77-1d98-415f-8701-01070651ecb0",
            "body": {
                "message": "The funding wallet does not have an initialized USDC token account. "
                "Deposit USDC and try again.",
                "error": "Bad Request",
                "statusCode": 400,
            },
        },
    }
}

ERROR_CONFIRMATION_UNKNOWN = {
    "error": {
        "code": "PENDING_INTENT_ERROR",
        "message": "The confirmation ID is unknown, expired, or has already been used.",
    }
}

ERROR_CONFIRMATION_EXPIRED = {
    "error": {
        "code": "PENDING_INTENT_ERROR",
        "message": "The prepared operation has expired. Prepare it again before submitting.",
    }
}

ERROR_CREDENTIAL = {
    "error": {"code": "CREDENTIAL_ERROR", "message": "No keypair configured for profile default."}
}

ERROR_INSUFFICIENT = {
    "error": {"code": "API_ERROR", "message": "Insufficient USDC balance to fund this task."}
}

ERROR_UNSUPPORTED_MINT = {
    "error": {"code": "API_ERROR", "message": "Unsupported payment mint address."}
}

ERROR_NOT_FOUND = {
    "error": {"code": "API_ERROR", "message": "Submission not found", "details": {"status": 404}}
}

SUBMIT_RESULT = {
    "taskId": PREPARE_RESULT["taskId"],
    "intentId": PREPARE_RESULT["intentId"],
    "signature": "5VfYmGB7qXhQfakeSignature1111111111111111111111111111111111111111111111111111",
    "status": "fulfilled",
}

REFUND_PREPARE_RESULT = {
    "confirmationId": "7c1d2e3f-4a5b-4c6d-8e7f-90a1b2c3d4e5",
    "intentId": "8d2e3f4a-5b6c-4d7e-9f80-a1b2c3d4e5f6",
    "taskId": TASK_ITEM["id"],
    "walletAddress": WALLET,
    "profile": "default",
    "environment": "stage",
    "createdAt": "2026-09-12T09:00:00.000Z",
    "expiresAt": "2026-09-12T09:05:00.000Z",
    "paymentQuote": {
        "token": {"mintAddress": USDC, "symbol": "USDC", "decimals": 6},
        "refundAmount": "10.00",
    },
    "nextAction": "Review the quote, then request human approval before calling gibwork_task_refund_submit.",
}
