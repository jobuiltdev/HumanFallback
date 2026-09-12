"""Deterministic, rule-based classifier.

Each rule is a regex tied to a category and a weight; every distinct match
of a rule contributes its weight. Human categories accumulate positive
scores; agent-capable verbs accumulate a counter-score.
A task is human-required when the strongest human category clears the
threshold and beats the agent-capable counter-score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from humanfallback.models import ClassificationResult, Signal, TaskCategory
from humanfallback.models.classification import HUMAN_CATEGORIES

NAME = "rules-v1"
DEFAULT_THRESHOLD = 0.5
MAX_CONFIDENCE = 0.95
MIN_CONFIDENCE = 0.05


@dataclass(frozen=True)
class Rule:
    category: TaskCategory
    label: str
    weight: float
    pattern: re.Pattern[str]


def _rule(category: TaskCategory, label: str, weight: float, pattern: str) -> Rule:
    return Rule(category, label, weight, re.compile(pattern, re.IGNORECASE))


C = TaskCategory

DEFAULT_RULES: tuple[Rule, ...] = (
    # Physical presence or handling of objects
    _rule(C.PHYSICAL_ACTION, "requires physical presence", 0.7,
          r"\b(in[- ]person|on[- ]site|physically|go to the|visit the|drive to|walk to)\b"),
    _rule(C.PHYSICAL_ACTION, "requires handling or moving an object", 0.7,
          r"\b(pick(?: it)? up|pickup|deliver|drop(?: it)? off|hand[- ]deliver|mail (?:the|a|this)|ship (?:the|a|this)|install (?:the|a))\b"),
    _rule(C.PHYSICAL_ACTION, "requires taking a photo of something real", 0.7,
          r"\b(take (?:a |some )?(?:photo|photos|picture|pictures)|photograph (?:the|a))\b"),
    # Identity checks that only the account holder can complete
    _rule(C.IDENTITY_VERIFICATION, "requires identity verification", 0.8,
          r"\b(kyc|verify (?:my|your|their) identity|identity verification|id verification|government[- ]issued id|passport|driver'?s licen[cs]e|selfie)\b"),
    # Judgement calls that need a person's taste or opinion
    _rule(C.SUBJECTIVE_JUDGMENT, "requires human taste or opinion", 0.6,
          r"\b(taste[- ]test|which (?:one )?(?:looks|sounds|feels|reads) (?:better|best)|human (?:opinion|feedback|review|judg(?:e)?ment)|rate (?:this|the|these)|does this feel|vibe check)\b"),
    # Actions on a personal account, or gated by a live second factor
    _rule(C.ACCOUNT_ACCESS, "requires the owner's account or a second factor", 0.8,
          r"\b(2fa|two[- ]factor|otp|one[- ]time (?:code|password)|captcha|authenticator app|log ?in to my|from my account)\b"),
    _rule(C.ACCOUNT_ACCESS, "requires posting from a personal social account", 0.6,
          r"\b(tweet|retweet|post (?:it |this )?on (?:x|twitter|instagram|linkedin|tiktok|facebook)|tag @|quote[- ]tweet)\b"),
    # Legally binding acts
    _rule(C.LEGAL_SIGNATURE, "requires a legally binding signature", 0.9,
          r"\b(sign (?:the|a|this|an) (?:contract|document|agreement|lease|nda|form|waiver)|notari[sz]e|wet signature|docusign|e-?sign)\b"),
    # Data that only exists offline
    _rule(C.OFFLINE_DATA_COLLECTION, "requires collecting data offline", 0.6,
          r"\b(survey|count (?:the|how many)|measure the|inspect the|check (?:the|whether the|if the) (?:store|shelf|site|building|shop|stock)|mystery shop)\b"),
    # Live conversations with other people
    _rule(C.HUMAN_INTERACTION, "requires a live conversation with a person", 0.6,
          r"\b(call (?:them|the|a|my) |phone call|negotiate|talk to|speak (?:with|to)|meet with|attend (?:the|a)|interview (?:the|a))\b"),
    # Counter-signals: work an agent can do on its own
    _rule(C.AGENT_CAPABLE, "agent-capable verb", 0.2,
          r"\b(write|refactor|summari[sz]e|translate|generate|draft|search|analy[sz]e|compute|calculate|convert|format|explain|list|fix (?:the )?bug|implement)\b"),
)


class RuleBasedClassifier:
    name = NAME

    def __init__(
        self,
        rules: tuple[Rule, ...] = DEFAULT_RULES,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self.rules = rules
        self.threshold = threshold

    def classify(self, text: str) -> ClassificationResult:
        signals = self._match(text)
        scores: dict[TaskCategory, float] = {}
        agent_score = 0.0
        for signal in signals:
            if signal.category is TaskCategory.AGENT_CAPABLE:
                agent_score += signal.weight
            else:
                scores[signal.category] = scores.get(signal.category, 0.0) + signal.weight

        if not scores:
            confidence = _clamp(0.6 + agent_score * 0.5)
            reasons = ["no human-required signals detected"]
            if agent_score:
                reasons.append("agent-capable verbs present")
            return ClassificationResult(
                human_required=False,
                category=TaskCategory.AGENT_CAPABLE,
                confidence=confidence,
                reasons=reasons,
                signals=signals,
                classifier=self.name,
            )

        top_category, top_score = max(scores.items(), key=lambda kv: kv[1])
        margin = top_score - agent_score
        human_required = top_score >= self.threshold and margin > 0
        reasons = [
            f"{s.label} (matched {s.matched_text!r})"
            for s in signals
            if s.category is top_category
        ]
        if human_required:
            confidence = _clamp(0.5 + margin * 0.5)
        else:
            confidence = _clamp(0.5 - margin * 0.5)
            reasons.append("agent-capable signals outweigh human-required signals")
        return ClassificationResult(
            human_required=human_required,
            category=top_category if human_required else TaskCategory.AGENT_CAPABLE,
            confidence=confidence,
            reasons=reasons,
            signals=signals,
            classifier=self.name,
        )

    def _match(self, text: str) -> list[Signal]:
        signals: list[Signal] = []
        for rule in self.rules:
            seen: set[str] = set()
            for match in rule.pattern.finditer(text):
                matched = match.group(0)
                key = matched.lower()
                if key in seen:
                    continue
                seen.add(key)
                signals.append(
                    Signal(
                        category=rule.category,
                        label=rule.label,
                        weight=rule.weight,
                        matched_text=matched,
                    )
                )
        return signals


def _clamp(value: float) -> float:
    return round(min(MAX_CONFIDENCE, max(MIN_CONFIDENCE, value)), 2)


def default_classifier() -> RuleBasedClassifier:
    return RuleBasedClassifier()


__all__ = ["DEFAULT_RULES", "HUMAN_CATEGORIES", "Rule", "RuleBasedClassifier", "default_classifier"]
