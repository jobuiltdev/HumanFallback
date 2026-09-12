from __future__ import annotations

import pytest

from humanfallback.classifier import RuleBasedClassifier, default_classifier
from humanfallback.models import TaskCategory

C = TaskCategory


@pytest.fixture(scope="module")
def clf() -> RuleBasedClassifier:
    return default_classifier()


HUMAN_CASES = [
    ("Go to the post office and pick up the package for me.", C.PHYSICAL_ACTION),
    ("Take a photo of the storefront on 5th Avenue.", C.PHYSICAL_ACTION),
    ("Complete the KYC check on the exchange.", C.IDENTITY_VERIFICATION),
    ("Upload a selfie to verify my identity.", C.IDENTITY_VERIFICATION),
    ("Which one looks better, the blue logo or the green one?", C.SUBJECTIVE_JUDGMENT),
    ("Approve the login by entering the 2FA code from my phone.", C.ACCOUNT_ACCESS),
    ("Tweet this announcement and tag @gibwork.", C.ACCOUNT_ACCESS),
    ("Sign the lease agreement and send it back.", C.LEGAL_SIGNATURE),
    ("Notarize the affidavit at the courthouse.", C.LEGAL_SIGNATURE),
    ("Count how many chairs are in the conference room.", C.OFFLINE_DATA_COLLECTION),
    ("Call the supplier and negotiate a better price.", C.HUMAN_INTERACTION),
]

AGENT_CASES = [
    "Refactor the auth module and write unit tests.",
    "Summarize this article in three bullet points.",
    "Translate the README into Spanish.",
    "Compute the monthly totals from this CSV.",
    "",
]


@pytest.mark.parametrize(("text", "category"), HUMAN_CASES)
def test_detects_human_required(clf: RuleBasedClassifier, text: str, category: C) -> None:
    result = clf.classify(text)
    assert result.human_required is True
    assert result.category is category
    assert result.confidence > 0.5
    assert result.reasons
    assert result.classifier == "rules-v1"


@pytest.mark.parametrize("text", AGENT_CASES)
def test_agent_capable(clf: RuleBasedClassifier, text: str) -> None:
    result = clf.classify(text)
    assert result.human_required is False
    assert result.category is C.AGENT_CAPABLE
    assert result.reasons


def test_strong_human_signal_beats_agent_verbs(clf: RuleBasedClassifier) -> None:
    result = clf.classify("Write a summary after you visit the site and take a photo.")
    assert result.human_required is True
    assert result.category is C.PHYSICAL_ACTION


def test_many_agent_verbs_outweigh_weak_human_signal(clf: RuleBasedClassifier) -> None:
    text = "Write, refactor, analyze, format and explain the code, then rate this approach."
    result = clf.classify(text)
    assert result.human_required is False
    assert any("outweigh" in r for r in result.reasons)


def test_signals_record_matched_text(clf: RuleBasedClassifier) -> None:
    result = clf.classify("Please DocuSign the NDA today.")
    labels = {s.label for s in result.signals}
    assert "requires a legally binding signature" in labels
    assert any(s.matched_text.lower() == "docusign" for s in result.signals)


def test_case_insensitive(clf: RuleBasedClassifier) -> None:
    assert clf.classify("COMPLETE THE KYC").human_required is True


def test_confidence_is_bounded(clf: RuleBasedClassifier) -> None:
    for text, _ in HUMAN_CASES:
        c = clf.classify(text).confidence
        assert 0.05 <= c <= 0.95


def test_threshold_is_configurable() -> None:
    strict = RuleBasedClassifier(threshold=0.99)
    assert strict.classify("Sign the contract.").human_required is False


def test_result_serialises(clf: RuleBasedClassifier) -> None:
    result = clf.classify("Pick up the keys.")
    data = result.model_dump(mode="json")
    assert data["category"] == "physical_action"
    assert isinstance(data["signals"], list)
