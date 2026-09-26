"""Unit tests for model selection.

``selection.py`` decides which model names an audit actually tests when the user
does not pass ``--models``. Until now nothing in the suite touched it: every call
site in ``tests/test_mock_relay.py`` passes an explicit model list, because those
tests are about probe behaviour rather than about picking. The one module whose
failure mode is *silent* was the one module without a test.

Silent is the whole problem. A probe that rejects something is loud — it prints a
finding with evidence. A bad selection is not: hand the twin-detection probes
five aliases of a single backend and they spend their budget comparing a model
against itself, find nothing, and the report says the relay looks clean. Wrong
selection therefore degrades the audit into a false-negative generator with no
visible symptom, which is why the rules below are asserted as equalities on
concrete name lists rather than as "returns something reasonable".

Everything here is a pure function over a list of strings: no server, no
requests, no sleeps.

Run with pytest, or directly::

    python tests/test_selection.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaycheck.cli import _make_output_safe  # noqa: E402
from relaycheck.families import claimed_family  # noqa: E402
from relaycheck.selection import (  # noqa: E402
    describe_selection,
    is_chat_model,
    select_models,
)

# Same reason as in ``test_mock_relay.py``: failure messages here are Chinese and
# a legacy console code page (cp1252 on the GitHub windows-latest runner) cannot
# encode them, so an unguarded print() inside a check turns a real assertion
# failure into an unreadable UnicodeEncodeError.
_make_output_safe()

#: Five names that claim OpenAI and three that claim three other vendors. This is
#: the shape of a real relay catalogue, and the reason ``selection.py`` exists.
ALIAS_HEAVY = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
    "gpt-3.5-turbo",
    "chatgpt-4o-latest",
    "claude-3-5-sonnet",
    "gemini-1.5-pro",
    "deepseek-chat",
]


# ------------------------------------------------------------ the chat filter


def test_non_chat_models_are_excluded() -> None:
    """A catalogue is mostly not chat models, and probing one proves nothing.

    Sending a chat completion to ``text-embedding-3-small`` returns an error, and
    an error is not evidence of fraud. Filtering them out is what keeps the audit
    from spending its budget on requests that cannot produce a finding.

    ``gpt-4o-audio-preview`` is included on purpose: it is a real OpenAI name and
    it *is* filtered, because the marker match is a heuristic over the name. A
    user who wants it audited can always pass ``--models`` and bypass selection.
    """
    excluded = [
        "text-embedding-3-small",
        "whisper-1",
        "tts-1",
        "stt-realtime",
        "dall-e-3",
        "stable-diffusion-xl",
        "flux-1-schnell",
        "sora-1",
        "bge-m3",
        "text-similarity-ada",
        "rerank-v1",
        "omni-moderation-latest",
        "gpt-4o-audio-preview",
        "Whisper-Large",  # marker matching must be case-insensitive
    ]
    still_chat = [name for name in excluded if is_chat_model(name)]
    assert not still_chat, "以下非对话模型没有被过滤掉：" + ", ".join(still_chat)


def test_ordinary_chat_names_are_kept() -> None:
    """The filter must not eat the models the audit is actually for."""
    kept = [
        "gpt-4o",
        "claude-3-5-sonnet",
        "gemini-1.5-pro",
        "deepseek-chat",
        "qwen-max",
        "glm-4",
        "llama-3.3-70b",
        "mistral-large",
        "grok-2",
        "yi-large",
        "doubao-pro",
        "ernie-4",
    ]
    dropped = [name for name in kept if not is_chat_model(name)]
    assert not dropped, "以下对话模型被误判为非对话模型：" + ", ".join(dropped)


# ------------------------------------------------------- the explicit override


def test_explicit_list_replaces_discovery() -> None:
    """``--models`` is an instruction, not a hint.

    The CLI warns when an explicit name is absent from ``/v1/models`` but still
    tries it (``cli.py:243``), because relays routinely hide names from that
    endpoint. Selection must agree: an explicit list is returned as given.
    """
    picked = select_models(ALIAS_HEAVY, 6, explicit=["llama-3-70b"])
    assert picked == ["llama-3-70b"], picked


def test_explicit_list_is_truncated_to_the_limit() -> None:
    picked = select_models(
        ALIAS_HEAVY,
        2,
        explicit=["gpt-4o", "claude-3-5-sonnet", "gemini-1.5-pro"],
    )
    assert picked == ["gpt-4o", "claude-3-5-sonnet"], picked


def test_non_positive_limit_keeps_the_whole_explicit_list() -> None:
    """``limit <= 0`` means "no limit" for an explicit list (``selection.py:57``).

    The CLI never passes 0 — it clamps with ``max(1, args.max_models)`` — so this
    documents the function's own contract rather than a reachable CLI state.
    """
    explicit = ["gpt-4o", "claude-3-5-sonnet"]
    assert select_models(ALIAS_HEAVY, 0, explicit=explicit) == explicit
    assert select_models(ALIAS_HEAVY, -1, explicit=explicit) == explicit


def test_explicit_blank_entries_are_dropped() -> None:
    """An empty string is not a model name.

    Whitespace-only entries are *not* stripped here, which is safe only because
    ``cli._split`` (``cli.py:360``) already strips and drops blanks before the
    list reaches this function. The assertion pins the boundary so that a future
    caller passing raw ``sys.argv`` values is caught.
    """
    picked = select_models(
        ALIAS_HEAVY, 5, explicit=["", "claude-3-5-sonnet", ""]
    )
    assert picked == ["claude-3-5-sonnet"], picked


# --------------------------------------------------- family-diverse discovery


def test_aliases_do_not_fill_every_slot() -> None:
    """The core rule: one slot per claimed vendor, before any vendor repeats.

    A relay advertising 30 names is usually a handful of real backends wearing
    aliases. Taking the first N in catalogue order hands the twin probes five
    names for one backend, and every comparison they make is that backend against
    itself — a guaranteed "no substitution found".

    The naive result here is ``ALIAS_HEAVY[:3]``, which is three OpenAI names.
    """
    picked = select_models(ALIAS_HEAVY, 3)

    assert picked == ["gpt-4o", "claude-3-5-sonnet", "gemini-1.5-pro"], picked
    assert picked != ALIAS_HEAVY[:3], "选择退回了列表顺序，等于没有选择"
    families = [claimed_family(name) for name in picked]
    assert len(set(families)) == len(picked), f"前 3 个槽位出现了同厂重复：{families}"


def test_second_round_starts_only_after_every_family_has_one() -> None:
    """Diversity first, depth second — and stop when the catalogue runs out.

    With more slots than families, the fill pass must round-robin at depth 1
    across *all* families before taking a second name from any one of them, and
    must terminate when every bucket is exhausted rather than loop forever.
    """
    available = [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4-turbo",
        "claude-3-5-sonnet",
        "claude-3-haiku",
    ]
    picked = select_models(available, 6)

    assert picked == [
        "gpt-4o",
        "claude-3-5-sonnet",
        "gpt-4o-mini",
        "claude-3-haiku",
        "gpt-4-turbo",
    ], picked
    assert len(picked) == len(available), "漏掉了目录里存在的模型"


def test_unknown_names_are_treated_as_separate_families() -> None:
    """An unrecognised name is its own bucket, not a shared "unknown" bucket.

    Grouping every unrecognised name together would make them look like aliases
    of one backend and collapse the selection to a single representative — the
    exact failure this module exists to avoid, applied to new model names, which
    is where it would do the most damage.
    """
    available = ["acme-alpha", "acme-beta", "zzz-model"]
    assert [claimed_family(n) for n in available] == [None, None, None]

    picked = select_models(available, 2)
    assert picked == ["acme-alpha", "acme-beta"], picked


def test_all_non_chat_list_still_yields_candidates() -> None:
    """If the filter empties the catalogue, fall back to the raw list.

    Returning nothing here would reach ``cli.py:253`` and abort the audit with
    "没有可审计的模型" — turning a filter heuristic into a hard failure on a relay
    that only advertises, say, embeddings and a reranker.
    """
    available = ["text-embedding-3-small", "whisper-1"]
    picked = select_models(available, 5)
    assert picked == available, picked


def test_empty_catalogue_yields_nothing() -> None:
    """No catalogue is an empty selection, not a crash — the CLI reports it."""
    assert select_models([], 5) == []


# ------------------------------------------------------------- determinism


def test_ambiguous_names_resolve_the_same_way_every_time() -> None:
    """A name claiming two vendors must classify identically across runs.

    ``claimed_family`` resolves a name that names two vendors by the longest
    matching keyword, and breaks a genuine tie by sorted order, so
    ``openai/claude-3-5-sonnet`` — the vendor-prefixed style some relays use —
    always resolves to ``anthropic``. If it did not, the same catalogue audited
    twice would pick different models, and two reports of one relay would not be
    comparable.
    """
    assert claimed_family("openai/claude-3-5-sonnet") == "anthropic"

    first = select_models(ALIAS_HEAVY + ["openai/claude-3-5-sonnet"], 6)
    second = select_models(ALIAS_HEAVY + ["openai/claude-3-5-sonnet"], 6)
    assert first == second, f"同一目录两次选择结果不同：{first} vs {second}"


def test_a_distilled_model_name_belongs_to_the_vendor_it_is_named_after() -> None:
    """``deepseek-r1-distill-qwen-32b`` is sold by DeepSeek, not by Alibaba.

    Both vendors are named in the string. Classifying it as ``alibaba`` by
    sorted order made a response that said ``deepseek-r1`` look like a
    cross-vendor swap, which is a MEDIUM ``echo-100`` against a relay that did
    nothing wrong. The family whose own name occupies the most of the string
    wins, and both spellings must agree or the comparison is meaningless.
    """
    assert claimed_family("deepseek-r1-distill-qwen-32b") == "deepseek"
    assert claimed_family("deepseek-r1") == "deepseek"
    # Same vendor through the distillation, so echo must not call it a swap.
    from relaycheck.probes.echo import _classify

    verdict, family = _classify("deepseek-r1-distill-qwen-32b", "deepseek-r1")
    assert verdict == "same_vendor", verdict
    assert family == "deepseek"


def test_selection_is_deterministic_across_repeats() -> None:
    for limit in (1, 2, 4, 6, 9):
        assert select_models(ALIAS_HEAVY, limit) == select_models(ALIAS_HEAVY, limit)


def test_limit_of_one_picks_the_first_family() -> None:
    """``--max-models 1`` is legal and must still return exactly one name."""
    picked = select_models(ALIAS_HEAVY, 1)
    assert picked == ["gpt-4o"], picked


# ------------------------------------------------------------------ rendering


def test_describe_selection_marks_unknown_families() -> None:
    """The progress line is the only place a user sees what was picked.

    ``cli.py:257`` prints it, and it must make an unrecognised name visibly
    unrecognised (``?``) instead of silently implying a vendor.
    """
    assert describe_selection(["gpt-4o", "acme-9"]) == "gpt-4o[openai], acme-9[?]"
    assert describe_selection([]) == ""


# --------------------------------------------------------------- standalone


def _main() -> int:
    checks = [
        test_non_chat_models_are_excluded,
        test_ordinary_chat_names_are_kept,
        test_explicit_list_replaces_discovery,
        test_explicit_list_is_truncated_to_the_limit,
        test_non_positive_limit_keeps_the_whole_explicit_list,
        test_explicit_blank_entries_are_dropped,
        test_aliases_do_not_fill_every_slot,
        test_second_round_starts_only_after_every_family_has_one,
        test_unknown_names_are_treated_as_separate_families,
        test_all_non_chat_list_still_yields_candidates,
        test_empty_catalogue_yields_nothing,
        test_ambiguous_names_resolve_the_same_way_every_time,
        test_a_distilled_model_name_belongs_to_the_vendor_it_is_named_after,
        test_selection_is_deterministic_across_repeats,
        test_limit_of_one_picks_the_first_family,
        test_describe_selection_marks_unknown_families,
    ]
    failed = 0
    for check in checks:
        try:
            check()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {check.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {check.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {check.__name__}")
    print(f"\n{len(checks) - failed}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
