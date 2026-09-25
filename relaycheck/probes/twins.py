"""Twin detection — the strongest client-side test for model substitution.

The idea
--------
A relay can rename a model, re-price it, re-describe it on the model plaza, and
rewrite the ``model`` field in the request. What it cannot do is make two
*different* sets of weights produce byte-identical output on several open-ended
prompts at temperature 0.

So: send the same salted prompt set to every model, and compare the outputs
pairwise.

Why the prompts are open-ended
------------------------------
An earlier version used prompts with a single canonical answer ("write 1 to 20",
"compute 17*23"). That is a trap: two *genuinely different* models both answer
"1 2 3 ... 20", so the comparison would flag every honest relay as a fraud. The
test only works when the answer space is enormous, so that independent models
essentially never collide while one model at temperature 0 reproduces itself.
Hence: invented protocols, haiku, invented colours, an imaginary city.

Why every prompt is run twice
-----------------------------
Open-ended prompts expose a second failure mode: if a backend is *not*
reproducible, a cross-model difference proves nothing (it may just be noise),
and a cross-model *match* would be luck. So each prompt is sent twice to the
same model and only the prompts where the two runs agree byte-for-byte enter the
comparison. Unstable prompts are dropped and reported as a limitation rather
than silently producing a false negative.

Combined with the ``tokenizer`` probe this becomes decisive:

* same tokenizer + same outputs      -> almost certainly the same backend
* same tokenizer, different output   -> same family, different size (legitimate)
* different tokenizer, same outputs  -> a hard cache / canned answer

Every prompt carries a per-run salt so that a relay which caches by prompt text
cannot serve a stale answer to the whole comparison.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from ..client import RelayBudgetExceeded, RelayError
from ..models import Confidence, ProbeResult, Severity
from .base import Probe, ProbeContext
from ..families import claimed_family

#: Open-ended prompts. Each one has an effectively unbounded answer space, so
#: two independent models will not agree byte-for-byte by accident, while a
#: single model sampled at temperature 0 reproduces itself.
#:
#: Do not replace these with prompts that have a single correct answer: that
#: turns every honest relay into a false positive.
DETERMINISTIC_PROMPTS: tuple[tuple[str, str], ...] = (
    (
        "protocol",
        "Invent a name for a fictional network protocol, then describe in exactly "
        "18 words what it does. Output only one line in the form: "
        "<name>: <description>",
    ),
    (
        "haiku",
        "Write a haiku (three lines, 5-7-5 syllables) about packet loss. "
        "Output only the three lines.",
    ),
    (
        "colors",
        "Invent three colours that do not exist in any standard palette. Output one "
        "line per colour in the form: <name> - <six word description>",
    ),
    (
        "city",
        "Describe an imaginary city in exactly 25 words. Output only the description.",
    ),
)

#: Answers shorter than this are never treated as evidence of sameness.
_MIN_USEFUL_CHARS = 24


class TwinsProbe(Probe):
    name = "twins"
    description = "跨模型比对开放式回答，找出换了名字的同一个后端"

    DEFAULT_MAX_MODELS = 6

    def run(self, ctx: ProbeContext) -> ProbeResult:
        result = self._result()
        max_models = int(ctx.option("twins_max_models", self.DEFAULT_MAX_MODELS))
        models = list(ctx.models)[:max_models]
        if len(models) < 2:
            result.error = "需要至少 2 个模型才能做双胞胎比对"
            return result

        repeats = max(1, int(ctx.option("twins_repeats", 2)))
        salt = "RC" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))

        outputs: dict[str, dict[str, str]] = {}
        unstable: dict[str, list[str]] = {}
        empty: dict[str, list[str]] = {}
        failures: dict[str, str] = {}

        for model in models:
            if ctx.out_of_budget():
                self._note_budget(
                    result, ctx, f"已完成 {len(outputs)}/{len(models)} 个模型的行为采样。"
                )
                break
            per_prompt: dict[str, str] = {}
            shaky: list[str] = []
            blank: list[str] = []
            for key, prompt in DETERMINISTIC_PROMPTS:
                if ctx.out_of_budget():
                    self._note_budget(
                        result,
                        ctx,
                        f"采样 {model} 时超出预算（提示词 {key}，"
                        f"已完成 {len(outputs)}/{len(models)} 个模型）。",
                    )
                    break
                message = f"[session {salt}]\n{prompt}"
                runs: list[str] = []
                try:
                    for _ in range(repeats):
                        # ctx.ask, not ctx.client.chat: a reasoning model needs a
                        # budget large enough to actually emit visible text, or
                        # every prompt looks empty for a reason that has nothing
                        # to do with the relay.
                        comp = ctx.ask(
                            model,
                            [{"role": "user", "content": message}],
                            max_tokens=200,
                            temperature=0,
                        )
                        runs.append(comp.content)
                except RelayBudgetExceeded:
                    self._note_budget(
                        result,
                        ctx,
                        f"采样 {model} 时超出预算（提示词 {key}，"
                        f"已完成 {len(outputs)}/{len(models)} 个模型）。",
                    )
                    break
                except RelayError as exc:
                    failures[model] = exc.describe()
                    break
                if not _normalise(runs[0]):
                    # No visible text at all. A reasoning model that spent its
                    # whole max_tokens budget on hidden reasoning looks exactly
                    # like this. Filing it under "unstable" would read as an
                    # accusation that the backend is flaky — it is neither
                    # stable nor unstable, it is empty, and the cause is our own
                    # token budget.
                    blank.append(key)
                elif all(_normalise(runs[0]) == _normalise(r) for r in runs[1:]):
                    per_prompt[key] = runs[0]
                else:
                    # The backend does not reproduce itself on this prompt, so a
                    # comparison on it would be meaningless in either direction.
                    shaky.append(key)
            if per_prompt:
                outputs[model] = per_prompt
            if shaky:
                unstable[model] = shaky
            if blank:
                empty[model] = blank
            if ctx.out_of_budget():
                break

        result.data["salt"] = salt
        result.data["prompts"] = [k for k, _ in DETERMINISTIC_PROMPTS]
        result.data["repeats"] = repeats
        result.data["outputs"] = outputs
        if unstable:
            result.data["unstable_prompts"] = unstable
        if empty:
            result.data["empty_prompts"] = empty
        if failures:
            result.data["failures"] = failures

        if len(outputs) < 2:
            # Not a crash. A crash means the probe itself broke; this means the
            # relay never gave us two comparable backends. Reporting it as an
            # error hides the fact that the audit is incomplete, and gives the
            # reader nothing to act on.
            self._find(
                result,
                id="twins-000",
                title="双胞胎比对未能进行",
                severity=Severity.INFO,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"只有 {len(outputs)}/{len(models)} 个模型产出了可复现的输出"
                    "（至少需要 2 个才能互相比对）。这一项没有被检验。"
                    + (
                        "注意返回为空与「输出不稳定」是两件事："
                        "推理模型在 max_tokens 不足时只返回隐藏的 reasoning，"
                        "正文为空，这与中转站是否可靠无关。"
                        if empty
                        else ""
                    )
                ),
                evidence={
                    "models_with_output": sorted(outputs),
                    "models_requested": list(models),
                    "failures": failures,
                    "unstable_prompts": unstable,
                    "empty_prompts": empty,
                },
            )
            return result

        pairs = self._compare_pairs(outputs)
        result.data["pairs"] = pairs

        tokenizer_groups = ctx.option("tokenizer_groups") or []
        result.data["tokenizer_groups_considered"] = tokenizer_groups

        identical = [p for p in pairs if p["identical_prompts"] == p["compared_prompts"]]
        if not identical:
            # ``compared_prompts == 0`` means the two backends never produced a
            # prompt they both answered reproducibly. That is a hole, not a pass:
            # the CLEAN branch below claims "these are almost certainly different
            # weights", which needs an actual comparison behind it.
            compared = [p for p in pairs if p.get("compared_prompts", 0) > 0]
            if not compared:
                self._find(
                    result,
                    id="twins-000",
                    title="没有任何模型对完成了可比对",
                    severity=Severity.INFO,
                    confidence=Confidence.CONFIRMED,
                    summary=(
                        "所有模型对都没有一条双方都稳定复现的开放性提示，"
                        "因此无法做行为比对。这一项没有被检验，不是「没问题」。"
                    ),
                    evidence={"pairs": pairs, "unstable_prompts": unstable},
                )
                return result
            self._find(
                result,
                id="twins-clean",
                title="未发现行为上完全一致的模型对",
                severity=Severity.CLEAN,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"{len(compared)} 个模型对在所有可复现的开放性测试提示上，"
                    "没有任何一对输出完全一致。"
                    "这说明这些模型极可能确实由不同的权重提供。"
                ),
                evidence={
                    "pairs": pairs,
                    "pairs_compared": len(compared),
                    "unstable_prompts": unstable,
                },
            )
            return result

        for pair in identical:
            a, b = pair["models"]
            fam_a, fam_b = claimed_family(a), claimed_family(b)
            different_family = bool(fam_a and fam_b and fam_a != fam_b)
            tokenizer_match = any(a in g and b in g for g in tokenizer_groups)

            finding_id = "twins-100"
            if tokenizer_match and different_family:
                severity, confidence = Severity.CRITICAL, Confidence.CONFIRMED
                verdict = (
                    "两个模型的 tokenizer 指纹完全相同，且在全部开放性提示上输出逐字节一致，"
                    "但它们被作为不同厂商的模型出售。这是掉包的直接证据。"
                )
            elif different_family:
                severity, confidence = Severity.HIGH, Confidence.LIKELY
                verdict = (
                    "两个声称来自不同厂商的模型，在多个开放性提示上输出了逐字节一致的结果。"
                    "独立模型在开放性提示上重合的概率极低，这通常意味着同一个后端。"
                )
            elif tokenizer_match:
                # Same claimed vendor + shared tokenizer + byte-identical output.
                # This is what a relay listing `gpt-4o` and `chatgpt-4o-latest`
                # looks like, and that is honest: same weights, two labels. We
                # cannot separate that from a mild re-labelling scam using output
                # comparison alone, so we say so instead of accusing. Its own id
                # and a LOW severity keep it from tripping --fail-on high on an
                # honest endpoint, and from being confused with twins-100 in the
                # report's severity table.
                finding_id = "twins-101"
                severity, confidence = Severity.LOW, Confidence.SUSPECTED
                verdict = (
                    "两个名称属于同一厂商的模型，不仅 tokenizer 指纹相同，"
                    "在全部开放性提示上输出也逐字节一致。这**可能是合法的同义词别名**"
                    "（同一套权重挂在两个名字下，例如 gpt-4o 与 chatgpt-4o-latest），"
                    "也可能是把一个后端当成两个档位卖。仅凭输出比对无法区分这两种情况，"
                    "因此这里不下结论。若你把它们当作不同档位付了不同的价钱，值得追问。"
                )
            else:
                severity, confidence = Severity.MEDIUM, Confidence.SUSPECTED
                verdict = (
                    "两个模型的输出完全一致。若它们声称是不同规模/不同代际的模型，"
                    "这不正常；若只是同一模型的不同别名，则属正常。"
                )

            self._find(
                result,
                id=finding_id,
                title=f"疑似同一后端：{a} ≈ {b}",
                severity=severity,
                confidence=confidence,
                summary=(
                    f"{verdict}（比对 {pair['compared_prompts']} 个提示，"
                    f"其中每个提示对两个模型各采样 {repeats} 次，全部逐字一致）"
                ),
                evidence={
                    "models": [a, b],
                    "claimed_families": [fam_a, fam_b],
                    "tokenizer_identical": tokenizer_match,
                    "identical_prompts": pair["identical_prompts"],
                    "compared_prompts": pair["compared_prompts"],
                    "repeats_per_prompt": repeats,
                    "sample": pair["sample"],
                },
                remediation=(
                    "要求中转站出示两个模型各自的上游调用凭据与账单；"
                    "若为同一后端，即为以次充好，可依据宣传不实要求退赔。"
                ),
            )

        return result

    # ------------------------------------------------------------------ internals

    def _compare_pairs(self, outputs: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
        names = sorted(outputs)
        pairs: list[dict[str, Any]] = []
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                va, vb = outputs[a], outputs[b]
                # Only compare prompts where *both* models reproduced themselves.
                shared = sorted(set(va) & set(vb))
                identical = [k for k in shared if _normalise(va[k]) == _normalise(vb[k])]
                sample_key = identical[0] if identical else (shared[0] if shared else None)
                pairs.append({
                    "models": [a, b],
                    "compared_prompts": len(shared),
                    "identical_prompts": len(identical),
                    "identical_keys": identical,
                    "sample": (
                        {"prompt": sample_key, "output": (va.get(sample_key) or "")[:300]}
                        if sample_key
                        else None
                    ),
                })
        return sorted(pairs, key=lambda p: p["identical_prompts"], reverse=True)


def _normalise(text: str) -> str:
    """Whitespace-insensitive comparison, but still byte-level on content."""
    if not text or len(text.strip()) < _MIN_USEFUL_CHARS:
        # Too short to be meaningful evidence ("ok" == "ok" proves nothing).
        return ""
    return re.sub(r"\s+", " ", text).strip()
