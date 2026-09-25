"""Core result types for relaycheck.

The whole tool is built around one idea: every probe produces zero or more
:class:`Finding` objects, and every finding carries its own evidence so a reader
can verify the claim without re-running the probe.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """How much the finding should worry you."""

    CRITICAL = "critical"   # you are being actively defrauded, with proof
    HIGH = "high"           # strong, verified evidence of misconduct
    MEDIUM = "medium"       # real problem, but needs a precondition or is narrow
    LOW = "low"             # quality / honesty issue, not outright theft
    INFO = "info"           # observation worth recording, not necessarily bad
    CLEAN = "clean"         # probe ran and found nothing wrong


#: Sort order for reports (worst first).
SEVERITY_ORDER: dict[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
    Severity.CLEAN: 5,
}


class Confidence(str, Enum):
    """How sure the probe is about its own finding.

    We keep this separate from severity on purpose. A confirmation-tier finding
    that is only *suspected* should not be reported as fact.
    """

    CONFIRMED = "confirmed"   # directly observed, reproducible from the evidence
    LIKELY = "likely"         # strong inference from observed data
    SUSPECTED = "suspected"   # a signal worth a human looking at


@dataclass
class Finding:
    """One thing a probe noticed."""

    id: str
    title: str
    severity: Severity
    confidence: Confidence
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    remediation: str | None = None

    def sort_key(self) -> tuple[int, int]:
        return (SEVERITY_ORDER[self.severity], 0 if self.confidence is Confidence.CONFIRMED else 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "severity": self.severity.value,
            "confidence": self.confidence.value,
            "summary": self.summary,
            "evidence": self.evidence,
            "remediation": self.remediation,
        }


@dataclass
class ProbeResult:
    """Outcome of running one probe."""

    probe: str
    findings: list[Finding] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    requests_made: int = 0
    duration_s: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def add(self, finding: Finding) -> Finding:
        self.findings.append(finding)
        return finding

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe": self.probe,
            "ok": self.ok,
            "error": self.error,
            "requests_made": self.requests_made,
            "duration_s": round(self.duration_s, 3),
            "findings": [f.to_dict() for f in self.findings],
            "data": self.data,
        }


@dataclass
class Usage:
    """Token accounting for one completion, normalised across providers.

    Relays disagree wildly on field names, so we accept several spellings and
    record whichever were present.
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None        # OpenAI: prompt_tokens_details.cached_tokens
    cache_read_tokens: int | None = None    # Anthropic-style relays
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None     # completion_tokens_details.reasoning_tokens
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "Usage":
        u = cls(raw=payload or {})
        if not payload:
            return u

        u.prompt_tokens = _first_int(payload, "prompt_tokens", "input_tokens")
        u.completion_tokens = _first_int(payload, "completion_tokens", "output_tokens")
        u.total_tokens = _first_int(payload, "total_tokens")
        u.cache_read_tokens = _first_int(payload, "cache_read_input_tokens", "cache_read_tokens")
        u.cache_write_tokens = _first_int(payload, "cache_creation_input_tokens", "cache_write_tokens")

        details = payload.get("prompt_tokens_details")
        if isinstance(details, dict):
            u.cached_tokens = _first_int(details, "cached_tokens")

        cdetails = payload.get("completion_tokens_details")
        if isinstance(cdetails, dict):
            u.reasoning_tokens = _first_int(cdetails, "reasoning_tokens")

        return u

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass
class Completion:
    """One chat completion, normalised."""

    model_requested: str
    model_returned: str | None
    content: str
    usage: Usage
    finish_reason: str | None = None
    latency_s: float = 0.0
    id: str | None = None
    created: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    http_status: int = 200
    #: HTTP attempts this logical call took. >1 means the caller asked for more
    #: room after a reasoning model spent its whole budget before emitting any
    #: visible text — see :meth:`relaycheck.probes.base.ProbeContext.ask`.
    attempts: int = 1
    #: the ``max_tokens`` actually used on the final attempt
    max_tokens_used: int | None = None

    @property
    def visible_chars(self) -> int:
        return len(self.content)

    @property
    def visible_empty(self) -> bool:
        """True when the model returned no visible text at all.

        Reasoning models emit ``reasoning_content`` first, so a small
        ``max_tokens`` comes back as an empty ``content`` with
        ``finish_reason == "length"``. Two empty answers are *not* two
        different answers, and callers must not treat them as one.

        Neither this nor :attr:`budget_exhausted` is the starvation *test* on
        its own — the combination lives in
        :func:`relaycheck.probes.base.starved_of_visible_text`, which also
        covers a scrap of visible text. These two are here because "the model
        said nothing" and "the model was cut off" are different facts about a
        response, and a caller reading one of them should not have to infer it
        from the other.
        """
        return not self.content.strip()

    @property
    def budget_exhausted(self) -> bool:
        """True when generation stopped only because it ran out of tokens.

        A ``True`` here means the visible text is whatever the budget allowed,
        not whatever the model wanted to say — so it is not safe to compare
        this reply against one produced under a different budget.
        """
        return self.finish_reason == "length"

    @property
    def reasoning_tokens(self) -> int | None:
        return self.usage.reasoning_tokens

    def to_dict(self, include_content: bool = True) -> dict[str, Any]:
        d = {
            "model_requested": self.model_requested,
            "model_returned": self.model_returned,
            "finish_reason": self.finish_reason,
            "latency_s": round(self.latency_s, 3),
            "visible_chars": self.visible_chars,
            "usage": self.usage.to_dict(),
            "http_status": self.http_status,
            "attempts": self.attempts,
        }
        if self.max_tokens_used is not None:
            d["max_tokens_used"] = self.max_tokens_used
        if include_content:
            d["content"] = self.content
        return d


def _first_int(d: dict[str, Any], *keys: str) -> int | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return int(v)
    return None


class Stopwatch:
    """Tiny context manager for timing probe steps."""

    def __enter__(self) -> "Stopwatch":
        self._t0 = time.monotonic()
        self.elapsed = 0.0
        return self

    def __exit__(self, *exc: Any) -> None:
        self.elapsed = time.monotonic() - self._t0
