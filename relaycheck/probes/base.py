"""Probe framework.

A probe is a self-contained check that talks to the relay through a
:class:`ProbeContext` and returns a :class:`~relaycheck.models.ProbeResult`.

Probes must be:

* **read-only** — never mutate remote state;
* **cheap** — a full audit should cost cents, not dollars;
* **self-evidencing** — every finding carries the raw numbers behind it;
* **honest** — if a probe cannot reach a conclusion, it says so instead of
  guessing.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..client import RelayClient, RelayError
from ..models import Completion, Finding, ProbeResult

#: A reasoning model can burn the entire ``max_tokens`` budget on hidden
#: ``reasoning_content`` and return an empty ``content`` with
#: ``finish_reason == "length"``. :meth:`ProbeContext.ask` retries once with
#: this multiple of the original cap — and at least :data:`_GROW_MIN` tokens —
#: before accepting the empty answer.
_GROW_FACTOR = 4
_GROW_MIN = 1024


def grow_budget(current: int) -> int:
    """The larger ``max_tokens`` to retry with when a reply came back empty."""
    return max(current * _GROW_FACTOR, _GROW_MIN)


#: visible characters below which a length-truncated reply counts as starved.
#: Staying strictly above zero matters: a reasoning model given too small a
#: budget can spill a *few* characters ("I don'") after burning the rest on
#: hidden reasoning. That fragment is not an answer — comparing it against a
#: full one manufactured the very mismatch this module exists to avoid.
_STARVED_VISIBLE_CHARS = 24


def starved_of_visible_text(content: str, finish_reason: "str | None") -> bool:
    """Whether a reply was all hidden reasoning and no usable visible text.

    Both halves matter. A short ``content`` with ``finish_reason == "stop"`` is
    a model that chose to say little — retrying it just buys the same silence,
    so only a reply that was *cut off* counts as starved.

    The threshold is not zero because ``"length"`` with a handful of visible
    characters means the budget went to hidden reasoning, not to the answer.
    """
    if finish_reason != "length":
        return False
    return len((content or "").strip()) < _STARVED_VISIBLE_CHARS


@dataclass
class ProbeContext:
    """Everything a probe needs to do its job."""

    client: RelayClient
    models: Sequence[str]
    #: per-probe cap on billable model calls, to bound cost
    max_calls: int = 40
    #: free-form options from the CLI
    options: dict[str, Any] = field(default_factory=dict)

    #: wall-clock budget for the *current* probe, set by :func:`run_probes`.
    #: A flaky relay can otherwise stall a probe for tens of minutes: a single
    #: 45-second read timeout, retried, multiplied by dozens of requests.
    budget_s: float = 240.0
    _deadline: float = 0.0

    #: optional intra-probe progress sink, wired by :func:`run_probes`
    _progress: "Callable[[str], None] | None" = None

    def option(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)

    def progress(self, message: str) -> None:
        """Report progress *within* a probe.

        A probe looping over 8 models on a relay that times out takes minutes,
        and a silent tool looks hung — which is the complaint that made the
        wall-clock budget necessary in the first place. No-op unless the CLI
        wired a sink. Keep messages short and never include keys or prompts.
        """
        if self._progress is not None:
            self._progress(message)

    # ------------------------------------------------------------------ budget

    def start_budget(self, seconds: float | None = None) -> None:
        """(Re)start the current probe's wall-clock budget."""
        if seconds is not None:
            self.budget_s = float(seconds)
        self._deadline = time.monotonic() + max(1.0, self.budget_s)

    def budget_left(self) -> float:
        if not self._deadline:
            return float("inf")
        return self._deadline - time.monotonic()

    def out_of_budget(self) -> bool:
        """True once the probe has spent its wall-clock budget."""
        return self.budget_left() <= 0.0

    # ---------------------------------------------------------------- requests

    def ask(
        self,
        model: str,
        messages: Sequence[dict[str, Any]],
        *,
        max_tokens: int,
        grow: bool = True,
        **kwargs: Any,
    ) -> Completion:
        """One chat call that gives a reasoning model a second chance.

        Reasoning models (DeepSeek-R1 style) emit ``reasoning_content`` before
        any visible ``content``. With a small ``max_tokens`` the whole budget is
        consumed by that hidden reasoning, and the response comes back empty —
        or, worse, with a two-word scrap like ``"I don'"`` — under
        ``finish_reason == "length"``. Both are the same failure: the budget,
        not the model, decided what the visible text would be.

        Every probe that compares *visible text* is broken by this in a
        specific, dangerous way: two empty answers look like two *different*
        answers, so an honest relay that merely serves reasoning models gets
        accused of swapping models behind the stream. This helper asks again
        with a larger budget so the comparison has something real to work on.

        The bigger budget is nearly free in practice: ``max_tokens`` is a cap,
        not a reservation. A model that answers in 120 tokens still answers in
        120 tokens; only a model that was cut off spends more.

        The retry is skipped when the probe is out of wall-clock budget, when
        the answer was not cut short, and when it was long enough to work with —
        so a normal relay pays nothing. The returned completion records
        ``attempts`` and ``max_tokens_used`` for the report.
        """
        comp = self.client.chat(model, messages, max_tokens=max_tokens, **kwargs)
        comp.max_tokens_used = max_tokens
        if not grow or not starved_of_visible_text(comp.content, comp.finish_reason):
            return comp
        if self.out_of_budget():
            return comp

        bigger = grow_budget(max_tokens)
        self.progress(
            f"{model} 在 max_tokens={max_tokens} 下可见正文被 reasoning 挤没了"
            f"（剩 {len((comp.content or '').strip())} 字符），以 max_tokens={bigger} 重试一次"
        )
        try:
            retried = self.client.chat(model, messages, max_tokens=bigger, **kwargs)
        except RelayError:
            # The first call already answered; a failed *retry* must not turn a
            # usable observation into an error.
            comp.attempts = 2
            return comp
        retried.attempts = comp.attempts + 1
        retried.max_tokens_used = bigger
        return retried


class Probe(abc.ABC):
    """Base class for all probes."""

    #: short stable identifier, used in ``--probes`` and report sections
    name: str = "unnamed"
    #: one-line human description
    description: str = ""
    #: True when the probe needs an explicit list of models to compare
    needs_models: bool = False

    @abc.abstractmethod
    def run(self, ctx: ProbeContext) -> ProbeResult:
        """Execute the probe."""

    # ------------------------------------------------------------------ helper

    def _result(self) -> ProbeResult:
        return ProbeResult(probe=self.name)

    @staticmethod
    def _find(
        result: ProbeResult,
        *,
        id: str,
        title: str,
        severity,
        confidence,
        summary: str,
        evidence: dict[str, Any] | None = None,
        remediation: str | None = None,
    ) -> Finding:
        return result.add(
            Finding(
                id=id,
                title=title,
                severity=severity,
                confidence=confidence,
                summary=summary,
                evidence=evidence or {},
                remediation=remediation,
            )
        )


    def _note_budget(self, result: ProbeResult, ctx: ProbeContext, detail: str) -> None:
        """Record that a probe stopped early because the relay was too slow.

        This is never silent: a partially-run probe must not look like a probe
        that ran fully and found nothing. Idempotent — a probe that hits both a
        soft budget check and a raised :class:`RelayBudgetExceeded` still emits
        exactly one finding.
        """
        from ..models import Confidence, Severity

        if result.data.get("truncated"):
            return
        result.data["truncated"] = True
        result.data["truncated_detail"] = detail
        result.data["budget_s"] = ctx.budget_s
        self._find(
            result,
            id="budget-000",
            title=f"探针 {self.name} 因超时预算提前结束",
            severity=Severity.INFO,
            confidence=Confidence.CONFIRMED,
            summary=(
                f"{detail} 该探针在 {ctx.budget_s:.0f} 秒内只完成了部分检查，"
                "未完成的部分没有结论——不要把它当作已通过。"
                "若这是中转站响应过慢导致，请用 --budget 调大预算，"
                "或先用 --probes reliability 确认对方的可用性。"
            ),
            evidence={"probe": self.name, "budget_s": ctx.budget_s, "stopped_after": detail},
        )


def run_probes(
    probes: Sequence[Probe],
    ctx: ProbeContext,
    *,
    on_probe: "Callable[[Probe], None] | None" = None,
    on_result: "Callable[[Probe, ProbeResult], None] | None" = None,
    on_progress: "Callable[[str], None] | None" = None,
) -> list[ProbeResult]:
    """Run probes in order, isolating failures so one crash cannot kill an audit.

    ``on_probe`` fires before each probe and ``on_result`` after, so a CLI can
    report progress without the probes knowing anything about presentation.
    ``on_progress`` is handed to the context for *intra*-probe reporting via
    :meth:`ProbeContext.progress`.

    Each probe gets a fresh wall-clock budget (``ctx.budget_s``). Probes that loop
    over models or prompts must poll :meth:`ProbeContext.out_of_budget` and stop,
    calling :meth:`Probe._note_budget` — a slow relay must degrade the audit into
    "partially checked", never into "hung forever".
    """
    results: list[ProbeResult] = []
    saved_deadline = ctx.client.deadline
    saved_progress = ctx._progress
    ctx._progress = on_progress
    try:
        for probe in probes:
            if on_probe is not None:
                on_probe(probe)
            ctx.start_budget(ctx.option("probe_budget_s", ctx.budget_s))
            # The client enforces the same deadline inside every request, so a
            # probe that forgets to poll still cannot hang the audit.
            ctx.client.deadline = ctx._deadline
            t0 = time.monotonic()
            before = ctx.client.request_count
            try:
                res = probe.run(ctx)
            except Exception as exc:  # noqa: BLE001 - a probe must never kill the run
                res = ProbeResult(probe=probe.name, error=f"{type(exc).__name__}: {exc}")
            res.duration_s = time.monotonic() - t0
            res.requests_made = ctx.client.request_count - before
            results.append(res)
            if on_result is not None:
                on_result(probe, res)
    finally:
        ctx.client.deadline = saved_deadline
        ctx._progress = saved_progress
    return results
