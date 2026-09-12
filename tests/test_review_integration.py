"""Review through the service, CLI, and MCP server with the mock backend and
the fake Gibwork session. Asserts the review path never writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fakes import payloads as P
from fakes.session import FakeSession, FakeSessionFactory, returns
from humanfallback import cli
from humanfallback.adapters import GibworkMcpAdapter, MockGibworkAdapter
from humanfallback.cli import app
from humanfallback.mcp_server import PREFIX
from humanfallback.service import Service, ServiceCode, ServiceError
from humanfallback.store import ContractStore
from test_mcp_server import Harness

runner = CliRunner()
REQ = "Tweet this announcement and tag @gibwork, then send the link."
IMG = "https://cdn.gib.work/media/shot.png"
POST = "https://x.com/worker/status/12345"


@pytest.fixture
def mock_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Service, Path]:
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    state = tmp_path / "mock_gibwork.json"
    service = Service(
        store_factory=lambda: ContractStore(tmp_path / "humanfallback.db"),
        adapter_factory=lambda writes: MockGibworkAdapter(state_path=state),
        adapter_name="mock",
    )
    return service, state


def _delegate(service: Service) -> tuple[str, str]:
    c = service.create_contract(REQ, reward="2.00")
    with service.open_delegation(c.id) as s:
        s.prepare()
        s.submit()
    return c.id, service.get_contract(c.id).delegation.task_id  # type: ignore[union-attr]


class TestService:
    def test_review_one_all_compare(self, mock_env: tuple[Service, Path]) -> None:
        service, state = mock_env
        cid, task_id = _delegate(service)
        adapter = MockGibworkAdapter(state_path=state)
        good = adapter.seed_submission(task_id, content=f"Tweeted and tagged @gibwork: {POST}", submitter="ann", media=[IMG])
        bad = adapter.seed_submission(task_id, content="done", submitter="bob")

        review, backend = service.review_submission(cid, good.id)
        assert backend.adapter == "mock"
        assert review.submission_id == good.id and review.contract_id == cid
        assert review.recommendation.value == "strong"

        reviews, items, _ = service.review_all(cid)
        assert {r.submission_id for r in reviews} == {good.id, bad.id}
        assert len(items) == 2

        cmp, _ = service.compare_submissions(cid)
        assert cmp.strongest_submission_id == good.id
        assert [r.rank for r in cmp.ranked_reviews] == [1, 2]

        pending, _, _ = service.review_all(cid, status="approved")
        assert pending == []

    def test_errors(self, mock_env: tuple[Service, Path]) -> None:
        service, _ = mock_env
        c = service.create_contract(REQ)
        with pytest.raises(ServiceError) as exc:
            service.review_all(c.id)
        assert exc.value.code is ServiceCode.NOT_DELEGATED
        cid, _ = _delegate(service)
        with pytest.raises(ServiceError) as exc:
            service.review_submission(cid, "nope")
        assert exc.value.code is ServiceCode.NOT_FOUND

    def test_review_never_writes_on_gibwork(self, tmp_path: Path) -> None:
        session = FakeSession(
            {
                "gibwork_wallet_status": returns(P.WALLET_STATUS),
                "gibwork_task_create_prepare": returns(P.PREPARE_RESULT),
                "gibwork_task_create_submit": returns(P.SUBMIT_RESULT),
                "gibwork_submission_list": returns(P.SUBMISSION_LIST),
                "gibwork_submission_get": returns(P.SUBMISSION_ITEM),
            }
        )
        writes: list[bool] = []

        def factory(w: bool) -> GibworkMcpAdapter:
            writes.append(w)
            return GibworkMcpAdapter(command=["node", "bin.js"], writes=w, session_factory=FakeSessionFactory(session))

        service = Service(store_factory=lambda: ContractStore(tmp_path / "hf.db"), adapter_factory=factory, adapter_name="gibwork")
        cid, _ = _delegate(service)
        writes.clear()
        before = len(session.calls)
        review, _ = service.review_submission(cid, P.SUBMISSION_ITEM["id"])
        assert review.submitter == "meme_worker"
        cmp, _ = service.compare_submissions(cid)
        assert len(cmp.ranked_reviews) == 1
        assert writes == [False, False]
        during = session.calls[before:]
        assert {n for n, _, _ in during} == {
            "gibwork_wallet_status", "gibwork_submission_get", "gibwork_submission_list"
        }
        assert not any(money for _, _, money in during)


@pytest.mark.usefixtures("data_dir")
class TestCli:
    def _seeded(self) -> tuple[str, str, str]:
        out = runner.invoke(app, ["contract", "create", REQ, "--json"]).stdout
        cid = json.loads(out)["id"]
        res = runner.invoke(app, ["delegate", cid, "--confirm", "--yes", "--json"])
        task_id = json.loads(res.stdout)["delegation"]["task_id"]
        adapter = MockGibworkAdapter(state_path=cli.config.mock_state_path())
        good = adapter.seed_submission(task_id, content=f"Tweeted and tagged @gibwork: {POST}", submitter="ann", media=[IMG])
        bad = adapter.seed_submission(task_id, content="done", submitter="bob")
        return cid, good.id, bad.id

    def test_review_submission_scorecard(self) -> None:
        cid, good, _ = self._seeded()
        res = runner.invoke(app, ["review", "submission", cid, good])
        assert res.exit_code == 0, res.output
        out = res.output
        assert "score:           100/100" in out
        assert "recommendation:  strong" in out
        assert "human judgment:  required" in out
        assert "[+] ev-1" in out and "[?] ac-1" in out
        assert "score breakdown:" in out and "required_evidence" in out
        assert "Advisory only" in out

    def test_review_submission_json(self) -> None:
        cid, _, bad = self._seeded()
        res = runner.invoke(app, ["review", "submission", cid, bad, "--json"])
        data = json.loads(res.stdout)
        assert data["recommendation"] == "reject_candidate"
        assert any(f["code"] == "EMPTY_SUBMISSION" for f in data["flags"])

    def test_review_all_and_rank(self) -> None:
        cid, good, bad = self._seeded()
        res = runner.invoke(app, ["review", "all", cid])
        assert res.exit_code == 0
        assert res.output.count("submission:") == 2
        res = runner.invoke(app, ["review", "rank", cid])
        assert res.exit_code == 0, res.output
        assert f"strongest: {good}" in res.output
        assert "human action:" in res.output and "does not approve" in res.output
        res = runner.invoke(app, ["review", "rank", cid, "--json"])
        assert json.loads(res.stdout)["strongest_submission_id"] == good

    def test_review_errors(self) -> None:
        res = runner.invoke(app, ["review", "all", "missing"])
        assert res.exit_code == 1 and "no contract" in res.output
        cid, _, _ = self._seeded()
        res = runner.invoke(app, ["review", "submission", cid, "nope"])
        assert res.exit_code == 1 and "not found" in res.output

    def test_no_approve_or_reject_commands(self) -> None:
        res = runner.invoke(app, ["review", "--help"])
        assert "approve" not in res.output.lower().replace("never approves", "")
        assert "reject" not in res.output.lower().replace("rejects", "")


class TestMcp:
    @pytest.fixture
    def harness(self, mock_env: tuple[Service, Path]) -> tuple[Harness, str, str, str]:
        service, state = mock_env
        cid, task_id = _delegate(service)
        adapter = MockGibworkAdapter(state_path=state)
        good = adapter.seed_submission(task_id, content=f"Tweeted and tagged @gibwork: {POST}", submitter="ann", media=[IMG])
        bad = adapter.seed_submission(task_id, content="done", submitter="bob")
        return Harness(service), cid, good.id, bad.id

    def test_review_tools(self, harness: tuple[Harness, str, str, str]) -> None:
        h, cid, good, bad = harness
        body = h.ok("review_submission", contract_id=cid, submission_id=good)
        assert body["advisory"] is True
        assert "does not approve" in body["human_action_required"]
        assert body["review"]["recommendation"] == "strong"
        assert body["review"]["human_judgment_required"] is True
        assert body["backend"]["adapter"] == "mock"

        listed = h.ok("review_all", contract_id=cid)
        assert listed["total"] == 2
        assert {r["submission_id"] for r in listed["reviews"]} == {good, bad}

        ranked = h.ok("review_rank", contract_id=cid)
        assert ranked["comparison"]["strongest_submission_id"] == good
        assert ranked["comparison"]["ranked_reviews"][0]["rank"] == 1
        assert ranked["advisory"] is True

    def test_review_errors(self, harness: tuple[Harness, str, str, str]) -> None:
        h, cid, _, _ = harness
        assert h.err("review_submission", contract_id=cid, submission_id="nope")["code"] == "NOT_FOUND"
        assert h.err("review_all", contract_id="missing")["code"] == "NOT_FOUND"

    def test_contract_view_offers_review_actions(self, harness: tuple[Harness, str, str, str]) -> None:
        h, cid, _, _ = harness
        actions = h.ok("contract_get", contract_id=cid)["allowed_actions"]
        assert PREFIX + "review_all" in actions and PREFIX + "review_rank" in actions

    def test_review_tools_are_read_only(self, harness: tuple[Harness, str, str, str]) -> None:
        h, _, _, _ = harness
        for t in h.tools():
            if "review" in t.name:
                assert t.annotations.read_only_hint is True
                assert t.annotations.destructive_hint is False
