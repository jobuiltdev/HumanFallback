from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from humanfallback import __version__
from humanfallback.cli import app

runner = CliRunner()

PHYSICAL = "Go to the hardware store and take a photo of the shelf."
AGENT = "Refactor the auth module and write tests."


def _run(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.output


def _create(*extra: str) -> dict:
    code, out = _run("contract", "create", PHYSICAL, "--json", *extra)
    assert code == 0, out
    return json.loads(out)


@pytest.mark.usefixtures("data_dir")
class TestCli:
    def test_version(self) -> None:
        code, out = _run("--version")
        assert code == 0
        assert __version__ in out

    def test_classify_text(self) -> None:
        code, out = _run("classify", PHYSICAL)
        assert code == 0
        assert "human_required: True" in out
        assert "physical_action" in out

    def test_classify_json(self) -> None:
        code, out = _run("classify", AGENT, "--json")
        assert code == 0
        data = json.loads(out)
        assert data["human_required"] is False
        assert data["category"] == "agent_capable"

    def test_create_and_show(self) -> None:
        created = _create("--reward", "2.50", "--tag", "errand", "--tag", "photo")
        assert created["status"] == "ready"
        assert created["reward"]["amount"] == "2.50"
        assert created["tags"] == ["errand", "photo"]

        code, out = _run("contract", "show", created["id"])
        assert code == 0
        assert created["id"] in out
        assert "acceptance criteria:" in out
        assert "ev-1" in out

    def test_create_refuses_agent_capable(self) -> None:
        code, out = _run("contract", "create", AGENT)
        assert code == 1
        assert "--force" in out

    def test_create_force_yields_draft(self) -> None:
        code, out = _run("contract", "create", AGENT, "--force", "--json")
        assert code == 0
        assert json.loads(out)["status"] == "draft"

    def test_create_rejects_bad_reward(self) -> None:
        code, out = _run("contract", "create", PHYSICAL, "--reward", "5")
        assert code == 1
        assert "two decimals" in out

    def test_list_and_filters(self) -> None:
        created = _create()
        _run("contract", "create", AGENT, "--force")

        code, out = _run("contract", "list")
        assert code == 0
        assert out.count("\n") == 2

        code, out = _run("contract", "list", "--status", "ready", "--json")
        assert [c["id"] for c in json.loads(out)] == [created["id"]]

        code, out = _run("contract", "list", "--category", "physical_action", "--json")
        assert len(json.loads(out)) == 1

    def test_list_empty(self) -> None:
        code, out = _run("contract", "list")
        assert code == 0
        assert "no contracts" in out

    def test_show_missing(self) -> None:
        code, out = _run("contract", "show", "missing")
        assert code == 1
        assert "no contract" in out

    def test_delegate_dry_run(self, data_dir: Path) -> None:
        created = _create("--reward", "3.00")
        code, out = _run("delegate", created["id"])
        assert code == 0, out
        assert "total_debit:      3.00" in out
        assert "dry run" in out

        _, shown = _run("contract", "show", created["id"], "--json")
        assert json.loads(shown)["status"] == "ready"
        state = json.loads((data_dir / "mock_gibwork.json").read_text())
        assert state["balance"] == "100.00"
        assert state["tasks"] == {}

    def test_delegate_confirm(self, data_dir: Path) -> None:
        created = _create("--reward", "3.00")
        code, out = _run("delegate", created["id"], "--confirm", "--json")
        assert code == 0, out
        data = json.loads(out)
        assert data["status"] == "delegated"
        assert data["delegation"]["adapter"] == "mock"
        assert data["delegation"]["quote"]["total_debit"] == "3.00"

        state = json.loads((data_dir / "mock_gibwork.json").read_text())
        assert state["balance"] == "97.00"
        assert data["delegation"]["task_id"] in state["tasks"]

        code, out = _run("delegate", created["id"])
        assert code == 1
        assert "only READY" in out

    def test_delegate_rejects_draft(self) -> None:
        code, out = _run("contract", "create", AGENT, "--force", "--json")
        contract_id = json.loads(out)["id"]
        code, out = _run("delegate", contract_id)
        assert code == 1
        assert "draft" in out

    def test_delegate_reports_adapter_errors(self) -> None:
        created = _create("--reward", "5000.00")
        code, out = _run("delegate", created["id"], "--confirm")
        assert code == 1
        assert "INSUFFICIENT_FUNDS" in out

    def test_delegate_unknown_adapter(self) -> None:
        created = _create()
        code, out = _run("delegate", created["id"], "--adapter", "gibwork")
        assert code == 1
        assert "unknown adapter" in out

    def test_status(self, data_dir: Path) -> None:
        _create()
        code, out = _run("status")
        assert code == 0
        assert str(data_dir) in out
        assert "contracts: 1" in out
        assert "balance  100.00 USDC" in out
