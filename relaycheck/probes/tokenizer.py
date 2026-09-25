"""Tokenizer fingerprinting.

Why this works
--------------
Every model family ships with a specific tokenizer. Feed the *same* fixed string
to two models and compare ``usage.prompt_tokens``: identical accounting means
identical tokenization, which means the same tokenizer, which means the same
model family.

This is the cheapest high-signal probe in the toolkit, because

* it costs almost nothing (``max_tokens=1``, tiny prompts);
* it cannot be faked by prompt-level trickery — a relay cannot make DeepSeek's
  tokenizer produce GPT-4's counts without actually running GPT-4;
* it is *differential*: we never need an official reference to compare two
  models offered by the same relay.

Honesty about the limits
------------------------
Identical token counts prove the same **tokenizer**, not the same **weights**.
``gpt-4o`` and ``gpt-4o-mini`` share a tokenizer. So we report a tokenizer match
as *same family*, and let the ``twins`` probe look for weight-level identity via
deterministic output comparison.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..client import RelayBudgetExceeded, RelayError
from ..families import claimed_family
from ..models import Confidence, ProbeResult, Severity
from .base import Probe, ProbeContext

#: Fixed probe strings. DO NOT change these casually — changing them changes
#: every fingerprint and breaks comparability between audit runs. If you must
#: extend the set, append new keys and keep the old ones.
TOKENIZER_STRINGS: dict[str, str] = {
    "en": "The quick brown fox jumps over the lazy dog. " * 4,
    "cjk": "人工智能正在改变世界，大语言模型的出现让机器能够理解和生成自然语言。" * 4,
    "code": "def fib(n):\n    return n if n < 2 else fib(n-1) + fib(n-2)\n" * 4,
    "emoji": "🙂🚀🔥🎉🌟💡🧠🎯" * 8,
    "mixed": "Hello 世界 123 test 测试 café naïve → ∑ ≠ ½",
    "ws": "a b  c   d    e     f      g       h",
    "digits": "1234567890 " * 8,
    "json": '{"key": "value", "nested": {"a": [1, 2, 3]}, "flag": true}\n' * 3,
}

#: Baseline message used to measure chat-template overhead.
_BASELINE = "hi"


class TokenizerProbe(Probe):
    name = "tokenizer"
    description = "用固定测试串的 prompt_tokens 给每个模型打 tokenizer 指纹"
    needs_models = True

    #: how many models to fingerprint before stopping (cost control)
    DEFAULT_MAX_MODELS = 8

    def run(self, ctx: ProbeContext) -> ProbeResult:
        result = self._result()
        max_models = int(ctx.option("tokenizer_max_models", self.DEFAULT_MAX_MODELS))
        models = list(ctx.models)[:max_models]
        if len(ctx.models) > max_models:
            result.data["models_skipped"] = list(ctx.models)[max_models:]

        fingerprints: dict[str, dict[str, int]] = {}
        missing_usage: list[str] = []
        errors: dict[str, str] = {}

        for model in models:
            if ctx.out_of_budget():
                self._note_budget(
                    result,
                    ctx,
                    f"已完成 {len(fingerprints)}/{len(models)} 个模型的 tokenizer 指纹采集。",
                )
                break
            try:
                fp = self._fingerprint(ctx, model)
            except RelayBudgetExceeded:
                self._note_budget(
                    result,
                    ctx,
                    f"采集 {model} 的指纹时超出预算（已完成 "
                    f"{len(fingerprints)}/{len(models)} 个模型）。",
                )
                break
            except RelayError as exc:
                errors[model] = exc.describe()
                continue
            if not fp:
                missing_usage.append(model)
                continue
            fingerprints[model] = fp

        result.data["fingerprints"] = fingerprints
        result.data["strings"] = {k: len(v) for k, v in TOKENIZER_STRINGS.items()}
        if missing_usage:
            result.data["models_without_usage"] = missing_usage
        if errors:
            result.data["model_errors"] = errors

        if not fingerprints:
            # Two very different worlds produce an empty fingerprint set, and
            # they deserve different words: a relay that answered but stripped
            # ``usage`` is doing something deliberate, while a relay that never
            # answered at all is simply broken. Claiming the first when the
            # second happened would be inventing a motive out of a timeout.
            if errors and not missing_usage:
                summary = (
                    "所有模型的 tokenizer 指纹采集都失败了（请求本身没有成功返回，"
                    "详见 evidence.errors）。没有 usage.prompt_tokens 就无法做指纹比对，"
                    "这是本工具最可靠的检测手段。这不是「中转站没问题」，是「这一项没测成」。"
                )
            elif missing_usage and not errors:
                summary = (
                    "模型有正常返回，但响应里没有可用的 usage.prompt_tokens。"
                    "没有它就无法做 tokenizer 指纹比对（这是本工具最可靠的检测手段）。"
                    "剥离 usage 字段会同时让用户无法自查计费，这本身就是值得注意的信号。"
                )
            else:
                summary = (
                    "没有任何模型返回可用的 usage.prompt_tokens：一部分请求失败，"
                    "另一部分返回了缺失 usage 的响应（详见 evidence）。"
                    "没有它就无法做 tokenizer 指纹比对——这是本工具最可靠的检测手段。"
                )
            self._find(
                result,
                id="tok-000",
                title="无法采集 tokenizer 指纹",
                severity=Severity.INFO,
                confidence=Confidence.CONFIRMED,
                summary=summary,
                evidence={
                    "errors": errors,
                    "models_without_usage": missing_usage,
                    "fingerprints_collected": 0,
                },
            )
            return result

        if missing_usage:
            self._find(
                result,
                id="tok-001",
                title="部分模型不返回 usage.prompt_tokens",
                severity=Severity.MEDIUM,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"以下模型未返回可解析的 prompt_tokens：{', '.join(missing_usage)}。"
                    "缺失 usage 会让用户无法自查计费，也会让 tokenizer 指纹检测失效。"
                ),
                evidence={"models_without_usage": missing_usage},
                remediation="官方 OpenAI 兼容 API 必须返回 usage。缺少它属于兼容性缺陷。",
            )

        self._detect_identical_tokenizers(result, fingerprints)
        # Share the groups with later probes (twins) so they can escalate
        # severity when tokenizer identity and behavioural identity agree.
        ctx.options["tokenizer_groups"] = result.data.get(
            "identical_tokenizer_groups", []
        ) or []
        return result

    # ------------------------------------------------------------------ internals

    def _fingerprint(self, ctx: ProbeContext, model: str) -> dict[str, int]:
        """Return ``{string_key: prompt_tokens_delta}`` for one model."""
        baseline = self._prompt_tokens(ctx, model, _BASELINE)
        if baseline is None:
            return {}

        out: dict[str, int] = {}
        for key, text in TOKENIZER_STRINGS.items():
            tokens = self._prompt_tokens(ctx, model, text)
            if tokens is None:
                continue
            out[key] = tokens - baseline
        return out

    def _prompt_tokens(self, ctx: ProbeContext, model: str, text: str) -> int | None:
        """One minimal completion, returning ``usage.prompt_tokens``."""
        comp = ctx.client.chat(
            model,
            [{"role": "user", "content": text}],
            max_tokens=1,
            temperature=0,
        )
        return comp.usage.prompt_tokens

    def _detect_identical_tokenizers(
        self, result: ProbeResult, fingerprints: dict[str, dict[str, int]]
    ) -> None:
        """Flag model pairs whose tokenizer vectors are exactly equal."""
        names = sorted(fingerprints)
        groups: list[list[str]] = []
        used: set[str] = set()

        for i, a in enumerate(names):
            if a in used:
                continue
            group = [a]
            for b in names[i + 1:]:
                if b in used:
                    continue
                if self._vectors_equal(fingerprints[a], fingerprints[b]):
                    group.append(b)
                    used.add(b)
            if len(group) > 1:
                groups.append(group)
                used.add(a)

        if not groups:
            result.data["identical_tokenizer_groups"] = []
            self._find(
                result,
                id="tok-clean",
                title="未发现 tokenizer 相同的模型对",
                severity=Severity.CLEAN,
                confidence=Confidence.CONFIRMED,
                summary="所有被测模型的 tokenizer 指纹互不相同，没有发现疑似同源的情况。",
                evidence={"fingerprints": fingerprints},
            )
            return

        result.data["identical_tokenizer_groups"] = groups
        for group in groups:
            reference = fingerprints[group[0]]
            families = {claimed_family(m) for m in group}
            known = {f for f in families if f}
            # Only *contradicting* name claims are evidence. If every name that
            # resolves points at one vendor, or some names do not resolve at all,
            # we cannot show the relay lied about who made the model — and a
            # shared tokenizer on its own is ordinary behaviour. Requiring two
            # distinct claimed vendors keeps an unknown name from being read as a
            # conflicting one.
            same_claimed_vendor = len(known) <= 1

            if same_claimed_vendor:
                # An honest relay selling several tiers of one vendor's lineup
                # (gpt-4o / gpt-4o-mini / gpt-4-turbo) shares a tokenizer across
                # all of them, because the vendor ships one tokenizer per family.
                # Calling that fraud would put a MEDIUM finding on a perfectly
                # honest endpoint — the one outcome this tool must never produce.
                vendor = next(iter(known), None)
                if vendor:
                    title = f"同厂商模型共享 tokenizer（{vendor}）：{', '.join(group)}"
                    claim = (
                        f"它们的名称都自称来自 {vendor}，而同一厂商的不同档位"
                        "（如 gpt-4o 与 gpt-4o-mini）本来就共用 tokenizer，"
                    )
                else:
                    title = f"模型共享 tokenizer，但名称无法判定厂商：{', '.join(group)}"
                    claim = (
                        "这些名称无法判定出自哪家厂商，因此无法据此指控掉包——"
                        "共享 tokenizer 本身只是同家族的正常表现，"
                    )
                self._find(
                    result,
                    id="tok-101",
                    title=title,
                    severity=Severity.INFO,
                    confidence=Confidence.CONFIRMED,
                    summary=(
                        f"这些模型在全部 {len(reference)} 个固定测试串上返回了完全一致的 "
                        f"prompt_tokens，即使用同一个 tokenizer。{claim}"
                        "因此这**不是**掉包证据。要区分『同家族』和『同权重』，"
                        "请看 twins 探针的行为比对结果。"
                    ),
                    evidence={
                        "group": group,
                        "claimed_families": sorted(families),
                        "shared_tokenizer_is_expected": True,
                        "vector": reference,
                    },
                )
                continue

            self._find(
                result,
                id="tok-100",
                title=f"模型共享同一 tokenizer：{', '.join(group)}",
                severity=Severity.MEDIUM,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"这些模型在全部 {len(reference)} 个固定测试串上返回了完全一致的 "
                    "prompt_tokens，说明它们使用同一个 tokenizer（同一模型家族）。"
                    f"而它们自称的厂商并不相同（{sorted(families)}），"
                    "所以其中至少有一个名不副实。"
                    "注意：相同 tokenizer 只证明『同家族』，不等同于『同权重』——"
                    "请结合 twins 探针的行为比对一起判断。"
                ),
                evidence={
                    "group": group,
                    "claimed_families": sorted(families),
                    "shared_tokenizer_is_expected": False,
                    "vector": reference,
                    "string_lengths": {k: len(v) for k, v in TOKENIZER_STRINGS.items()},
                },
                remediation=(
                    "要求中转站解释这些模型为何共享 tokenizer；"
                    "若宣称来自不同厂商，即为虚假宣传。"
                ),
            )

    @staticmethod
    def _vectors_equal(a: dict[str, int], b: dict[str, int]) -> bool:
        if set(a) != set(b) or not a:
            return False
        return all(a[k] == b[k] for k in a)


def pack_vectors(fingerprints: dict[str, dict[str, int]]) -> Sequence[dict[str, Any]]:
    """Flatten fingerprints for CSV/JSON export (used by the reporter)."""
    keys = sorted({k for v in fingerprints.values() for k in v})
    rows = []
    for model, vec in sorted(fingerprints.items()):
        row: dict[str, Any] = {"model": model}
        row.update({k: vec.get(k) for k in keys})
        rows.append(row)
    return rows
