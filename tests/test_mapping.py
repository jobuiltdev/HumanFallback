from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fakes import payloads as P
from humanfallback.adapters import ErrorCode
from humanfallback.adapters import mapping as M


class TestAmounts:
    def test_base_units(self) -> None:
        assert M.base_units_to_amount("10000000", 6) == "10.00"
        assert M.base_units_to_amount("1", 6) == "0.00"
        assert M.base_units_to_amount("1500000", 6) == "1.50"

    def test_two_decimals(self) -> None:
        assert M.two_decimals("1") == "1.00"
        assert M.two_decimals("1.5") == "1.50"
        assert M.two_decimals(2) == "2.00"

    def test_parse_datetime(self) -> None:
        parsed = M.parse_datetime("2026-09-11T20:48:06.643Z")
        assert parsed == datetime(2026, 9, 11, 20, 48, 6, 643000, tzinfo=UTC)
        assert M.parse_datetime(None) is None
        assert M.parse_datetime("garbage") is None


class TestWallet:
    def test_maps_real_payload(self) -> None:
        status = M.map_wallet_status(P.WALLET_STATUS, adapter="gibwork")
        assert status.adapter == "gibwork"
        assert status.profile == "default"
        assert status.environment == "stage"
        assert status.wallet_address == P.WALLET
        assert status.writes_enabled is True

    def test_read_only(self) -> None:
        assert M.map_wallet_status(P.WALLET_STATUS_READ_ONLY, adapter="gibwork").writes_enabled is False


class TestPrepare:
    def test_maps_real_payload(self) -> None:
        r = M.map_prepare_result(P.PREPARE_RESULT)
        assert r.confirmation_id == "fa30e95b-69bb-4db5-888d-692848b15ea4"
        assert r.intent_id == "517a18d2-50be-4a49-b691-c40077615cad"
        assert r.task_id == "6fe2affc-97fe-4943-a4ca-7604b545c0e0"
        assert r.wallet_address == P.WALLET
        assert r.environment == "stage"
        assert r.expires_at - r.created_at == __import__("datetime").timedelta(minutes=5)
        assert r.last_valid_block_height == 424294625
        q = r.payment_quote
        assert q.token.mint_address == P.USDC
        assert q.token.symbol == "USDC"
        assert q.token.decimals == 6
        assert q.funding_amount == "1.00"
        assert q.platform_fee.percent == 0
        assert q.platform_fee.amount == "0.00"
        assert q.total_debit == "1.00"
        assert "gibwork_task_create_submit" in r.next_action

    def test_missing_timestamps_rejected(self) -> None:
        bad = {**P.PREPARE_RESULT, "expiresAt": None}
        with pytest.raises(Exception, match="timestamps"):
            M.map_prepare_result(bad)


class TestSubmit:
    def test_reads_signature_and_ids(self) -> None:
        r = M.map_submit_result(P.SUBMIT_RESULT, confirmation_id="c", task_id=None, intent_id=None)
        assert r.task_id == P.PREPARE_RESULT["taskId"]
        assert r.intent_id == P.PREPARE_RESULT["intentId"]
        assert r.signature.startswith("5VfYmGB7")
        assert r.status == "fulfilled"
        assert r.raw == P.SUBMIT_RESULT

    def test_falls_back_to_prepare_ids(self) -> None:
        r = M.map_submit_result({}, confirmation_id="c", task_id="t", intent_id="i")
        assert (r.task_id, r.intent_id, r.signature, r.status) == ("t", "i", "", "submitted")

    def test_no_task_id_anywhere_is_protocol_error(self) -> None:
        with pytest.raises(Exception) as exc:
            M.map_submit_result({}, confirmation_id="c", task_id=None, intent_id=None)
        assert exc.value.code is ErrorCode.PROTOCOL_ERROR


class TestRefundPrepare:
    def test_maps_quote_and_keeps_raw(self) -> None:
        r = M.map_refund_prepare(P.REFUND_PREPARE_RESULT, task_id="fallback")
        assert r.confirmation_id == P.REFUND_PREPARE_RESULT["confirmationId"]
        assert r.task_id == P.TASK_ITEM["id"]
        assert r.refund_amount == "10.00"
        assert r.token is not None and r.token.symbol == "USDC"
        assert r.expires_at is not None
        assert r.raw == P.REFUND_PREPARE_RESULT

    def test_minimal_payload(self) -> None:
        r = M.map_refund_prepare({"confirmationId": "x"}, task_id="t")
        assert r.task_id == "t"
        assert r.refund_amount is None
        assert r.token is None


class TestTask:
    def test_maps_real_item(self) -> None:
        t = M.map_remote_task(P.TASK_ITEM)
        assert t.id == P.TASK_ITEM["id"]
        assert t.title == "Create a TOKEN$HIT meme"
        assert t.tags == ["Social Media", "Design"]
        assert t.status == "CREATED"
        assert t.is_open is True
        assert t.token.decimals == 6
        assert t.funding_amount == "10.00"
        assert t.min_submission_amount == "1.00"
        assert t.deadline is None
        assert t.created_at.year == 2026
        assert t.total_submissions == 1
        assert t.max_submissions == 10

    def test_maps_task_get_item(self) -> None:
        t = M.map_remote_task(P.TASK_GET_ITEM)
        assert t.id == P.TASK_ITEM["id"]
        assert t.funding_amount == "10.00"
        assert t.min_submission_amount == "1.00"
        assert t.total_submissions == 3  # pending 2 + approved 1 + rejected 0
        assert t.max_submissions == 10
        assert t.can_refund is None

    def test_total_submissions_unknown_without_any_count(self) -> None:
        item = {k: v for k, v in P.TASK_GET_ITEM.items() if not k.startswith("taskSubmissions")}
        assert M.map_remote_task(item).total_submissions is None

    def test_is_open_derived_from_status(self) -> None:
        item = {**P.TASK_ITEM}
        del item["isOpen"]
        assert M.map_remote_task(item).is_open is True
        assert M.map_remote_task({**item, "status": "CLOSED"}).is_open is False

    def test_unwrap_list(self) -> None:
        assert M.unwrap_list(P.TASK_LIST) == [P.TASK_ITEM]
        assert M.unwrap_list(P.TASK_LIST_EMPTY) == []
        assert M.unwrap_list([P.TASK_ITEM]) == [P.TASK_ITEM]
        assert M.unwrap_list(None) == []


class TestSubmission:
    def test_maps_item(self) -> None:
        s = M.map_submission(P.SUBMISSION_ITEM, task_id="fallback")
        assert s.id == P.SUBMISSION_ITEM["id"]
        assert s.task_id == P.TASK_ITEM["id"]
        assert s.status == "pending"
        assert s.submitter == "meme_worker"
        assert s.media == ["https://cdn.gib.work/media/m1.png"]
        assert s.created_at is not None
        assert s.raw == P.SUBMISSION_ITEM

    def test_string_submitter_and_status_case(self) -> None:
        s = M.map_submission({"id": "1", "submitter": "abc", "status": "APPROVED"}, task_id="t")
        assert s.submitter == "abc"
        assert s.status == "approved"
        assert s.task_id == "t"


class TestErrors:
    @pytest.mark.parametrize(
        ("payload", "code"),
        [
            (P.ERROR_AMOUNT_RANGE, ErrorCode.AMOUNT_OUT_OF_RANGE),
            (P.ERROR_TOKEN_ACCOUNT, ErrorCode.MISSING_TOKEN_ACCOUNT),
            (P.ERROR_CONFIRMATION_UNKNOWN, ErrorCode.CONFIRMATION_INVALID),
            (P.ERROR_CONFIRMATION_EXPIRED, ErrorCode.CONFIRMATION_EXPIRED),
            (P.ERROR_CREDENTIAL, ErrorCode.CREDENTIAL_ERROR),
            (P.ERROR_INSUFFICIENT, ErrorCode.INSUFFICIENT_FUNDS),
            (P.ERROR_UNSUPPORTED_MINT, ErrorCode.UNSUPPORTED_MINT),
            (P.ERROR_NOT_FOUND, ErrorCode.NOT_FOUND),
            ({"error": {"code": "CONFIG_ERROR", "message": "bad profile"}}, ErrorCode.CONFIG_ERROR),
            ({"error": {"code": "NETWORK_ERROR", "message": "ECONNRESET"}}, ErrorCode.NETWORK_ERROR),
            ({"error": {"code": "API_ERROR", "message": "something else"}}, ErrorCode.API_ERROR),
        ],
    )
    def test_translation(self, payload: dict, code: ErrorCode) -> None:
        err = M.translate_error(payload)
        assert err.code is code
        assert err.message

    def test_details_preserved(self) -> None:
        err = M.translate_error(P.ERROR_AMOUNT_RANGE)
        assert err.details["requestId"] == "20597f90-02e0-4202-b4e4-8029b0343a57"
        assert err.details["status"] == 400

    def test_non_dict_payload(self) -> None:
        err = M.translate_error("plain failure")
        assert err.code is ErrorCode.API_ERROR
        assert "plain failure" in err.message
