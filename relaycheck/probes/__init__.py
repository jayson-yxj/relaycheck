"""Probe registry.

Order matters:

* ``reliability`` runs first. If the endpoint cannot serve trivial requests, the
  report must say the rest of the audit is incomplete rather than clean.
* ``echo`` runs next, right after the panel. It is the cheapest hard check in the
  tool — two requests per model — and it answers a question the reader needs
  *before* reading anything else: which model names does the upstream itself put
  on the record?
* ``tokenizer`` runs before ``twins`` because ``twins`` escalates its severity
  when tokenizer identity and behavioural identity agree, and it reads that from
  ``ProbeContext.options["tokenizer_groups"]``.
* ``context`` runs last. It sends prompts of thousands of tokens, so it is both
  the slowest and the most expensive probe; nothing else consumes its output, and
  a relay that is already known to be unreliable should not be charged for it
  before the cheaper probes have had their say.
"""

from __future__ import annotations

from typing import Sequence

from .base import Probe, ProbeContext, run_probes
from .billing import HiddenReasoningProbe, PanelBillingProbe
from .context import ContextProbe
from .echo import EchoProbe
from .identity import CanaryProbe, IdentityProbe
from .params import ParamsProbe
from .reliability import ReliabilityProbe
from .stream import StreamProbe
from .tokenizer import TokenizerProbe
from .twins import TwinsProbe

#: Registration order == execution order.
ALL_PROBES: tuple[Probe, ...] = (
    ReliabilityProbe(),
    PanelBillingProbe(),
    EchoProbe(),
    TokenizerProbe(),
    TwinsProbe(),
    IdentityProbe(),
    CanaryProbe(),
    HiddenReasoningProbe(),
    ParamsProbe(),
    StreamProbe(),
    ContextProbe(),
)

#: Probes run when the caller does not ask for a specific set. These cover the
#: substitution and billing questions at a modest request cost.
DEFAULT_PROBE_NAMES: tuple[str, ...] = (
    "reliability",
    "billing-panel",
    "echo",
    "tokenizer",
    "twins",
    "identity",
    "canary",
    "billing-hidden-reasoning",
)

#: Heavier probes, opt-in via ``--probes all``. ``context`` sends prompts of
#: thousands of tokens, which costs real money on a metered relay, so it is never
#: part of the default set.
HEAVY_PROBE_NAMES: tuple[str, ...] = ("params", "stream", "context")

# Fail loudly at import time if the default set ever drifts from the registry,
# rather than at the end of a user's first audit.
_registered = {p.name for p in ALL_PROBES}
_missing = [n for n in DEFAULT_PROBE_NAMES if n not in _registered]
if _missing:  # pragma: no cover - programming error, not user error
    raise RuntimeError(
        f"DEFAULT_PROBE_NAMES references unregistered probes: {_missing}; "
        f"registered: {sorted(_registered)}"
    )


def probe_names() -> list[str]:
    return [p.name for p in ALL_PROBES]


def select_probes(names: Sequence[str] | None) -> list[Probe]:
    """Resolve a list of probe names to probe instances.

    Accepts ``None``/empty for the default set, the literal ``all``, and any
    mix of individual names.
    """
    if not names:
        wanted = list(DEFAULT_PROBE_NAMES)
    else:
        cleaned = [str(n).strip() for n in names if str(n).strip()]
        if any(n.lower() == "all" for n in cleaned):
            return list(ALL_PROBES)
        wanted = cleaned

    by_name = {p.name: p for p in ALL_PROBES}
    unknown = [n for n in wanted if n not in by_name]
    if unknown:
        raise KeyError(
            f"unknown probe(s): {', '.join(unknown)}. available: {', '.join(probe_names())}"
        )
    selected = {n: by_name[n] for n in wanted}
    # Preserve registration order regardless of the order names were given.
    return [p for p in ALL_PROBES if p.name in selected]


__all__ = [
    "ALL_PROBES",
    "DEFAULT_PROBE_NAMES",
    "HEAVY_PROBE_NAMES",
    "Probe",
    "ProbeContext",
    "probe_names",
    "run_probes",
    "select_probes",
]