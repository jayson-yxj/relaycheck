"""Context-window honesty.

A relay that advertises a large context but quietly feeds the upstream only the
tail of your prompt is charging you for input the model never saw. Nothing in the
response gives it away: the answer is fluent, on topic, and the usage block bills
the full prompt you sent.

The test is a needle-in-the-haystack with a built-in control:

* two high-entropy codes are planted in one long document — one just after the
  opening line, one just before the question;
* the question, at the very end, asks for both.

Each code is twelve characters from a 32-symbol alphabet, so a model cannot guess
one and cannot invent one by accident. If a code comes back, that part of the
document *was* in its input.

* both codes back → the document arrived intact at this depth;
* only the tail code → the head was dropped (the classic "keep the last N tokens");
* only the head code → the tail was dropped;
* neither → inconclusive, and reported as inconclusive.

The shallowest depth doubles as the control. A model that cannot retrieve either
code from a short document cannot retrieve them from a long one either, so a
failure there says nothing about truncation and must never be reported as if it
did.

Cost warning: this probe is the expensive one. A single 32k-token prompt can cost
as much as the rest of the audit together, which is why it is opt-in, tests one
model by default, and stops climbing the ladder at the first sign of trouble.
"""

from __future__ import annotations

import random
import re
from typing import Any, Sequence

from ..client import RelayBudgetExceeded, RelayError
from ..models import Completion, Confidence, ProbeResult, Severity
from .base import Probe, ProbeContext

#: Default ladder, in approximate tokens. Ascending, because a relay that caps at
#: 8k is the common case and the cheap rung proves the model can do the task at
#: all. The top rung is where the ladder *stops*, not a claimed upper bound.
DEFAULT_SIZES: tuple[int, ...] = (2000, 8000, 32000)

#: The probe is expensive; one model is the default.
DEFAULT_MAX_MODELS = 1

#: Seeds the filler so two audits of the same target use the same document.
_SEED = 20240925

#: English filler runs about four characters per token. Only an estimate — the
#: number that matters is the ``prompt_tokens`` the relay itself reports back,
#: which the finding quotes alongside the requested depth.
_CHARS_PER_TOKEN = 4.0

#: Enough for two labelled codes plus a little slack. Reasoning models sometimes
#: spend output tokens on hidden thinking first, so a very small cap would turn
#: "the model was cut off" into a fake truncation signal.
_MAX_TOKENS = 128

_HEAD_LABEL = "ALPHA"
_TAIL_LABEL = "BETA"
_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ23456789"
_CODE_LEN = 12

#: Only these statuses mean "this request was too big". Anything else (a 503, a
#: read timeout) is an inconclusive outcome, never a context finding: reporting a
#: dead endpoint as "your input was silently truncated" would be exactly the
#: false accusation this tool exists to avoid.
_SIZE_REJECT_STATUSES = frozenset({413, 414, 422})

#: A 400 is ambiguous, so it only counts as a size rejection when the body says so.
_SIZE_REJECT_HINT = re.compile(
    r"context|token|length|too\s+(?:large|long)|maximum|exceed|payload|size",
    re.IGNORECASE,
)

_OPENING = (
    "Reference document. Read every line of it, then answer the question at the end."
)
_QUESTION = (
    "Question: this document carries exactly two labelled codes, one labelled "
    f"{_HEAD_LABEL} and one labelled {_TAIL_LABEL}. Answer with nothing but those "
    "two exact codes, comma separated, the "
    f"{_HEAD_LABEL} code first. If only one of them is visible to you, answer with "
    "only that one. Do not explain."
)

#: Frozen filler. Deliberately mundane, varied, and free of digits and of any
#: substring another probe's mock keys on (see ``tests/mock_relay.py``): repeated
#: text is fine, but text that looks like a port scan or a JSON request is not.
_FILLER: tuple[str, ...] = (
    "The archive keeps its ledgers in loose-leaf folders sorted by the month they were filed.",
    "A clerk copies each entry twice, once for the shelf and once for the basement.",
    "Rain fell on the depot roof for most of the afternoon and nobody minded.",
    "The northern warehouse holds spare parts for machines nobody repairs any longer.",
    "A thin line of rust runs along the seam of every tin stored on the upper rack.",
    "Someone left a chipped mug beside the ledger and it stayed there for a year.",
    "The index cards are typed, not written, which makes them easier to read but harder to correct.",
    "Every third shelf is empty, and the gaps have their own peculiar smell of dust.",
    "A bell in the corridor rings twice when the loading door is opened from outside.",
    "The caretaker sweeps the aisle in the same direction every morning without being asked.",
    "Old invoices are bundled with twine and stacked against the eastern wall.",
    "The lamp above the sorting table flickers whenever the compressor starts.",
    "A hand-written note warns that the second drawer sticks in humid weather.",
    "Boxes from the closed branch arrived last spring and have not been opened since.",
    "The floorboards creak in a rhythm that the night staff have learned to ignore.",
    "Someone measured the aisle with a length of cord and never took the cord away.",
    "A grey cat sleeps on the warmest crate and is fed by whoever arrives first.",
    "The inventory list was retyped three times and each copy disagrees with the others.",
    "A shallow puddle forms under the drain whenever the rain is heavy enough.",
    "The ledger's margin carries the initials of a supervisor nobody can identify.",
    "Cold air moves through the vent and lifts the corner of every loose page.",
    "Two crates were stacked the wrong way round and stayed that way for years.",
    "The stamp pad dried out, so the later entries carry only a faint impression.",
    "A shelf label fell behind the rack and was found much later, still legible.",
    "The doorway is narrow enough that a loaded trolley has to be turned sideways.",
    "Someone wrote a reminder on the wall in pencil and then tried to rub it out.",
    "The basement stairs are steep, and the light switch sits at the bottom rather than the top.",
    "A stack of blank forms waits on the counter in case the printer fails again.",
    "The window latch is broken in a way that lets the frame rattle in strong wind.",
    "Everything on the middle shelf was renumbered during a reorganisation nobody remembers.",
    "A delivery was signed for by a name that appears nowhere else in the records.",
    "The clock above the door runs a little fast and has done so for a long time.",
)


class ContextProbe(Probe):
    name = "context"
    description = "检查长输入是否在到达模型之前被静默截断"

    def run(self, ctx: ProbeContext) -> ProbeResult:
        result = self._result()
        sizes = self._sizes(ctx)
        max_models = max(1, int(ctx.option("context_max_models", DEFAULT_MAX_MODELS)))
        models = list(ctx.models)[:max_models]

        rng = random.Random(_SEED)
        matrix: dict[str, dict[str, Any]] = {}
        truncated: list[tuple[str, dict[str, Any], list[int]]] = []
        rejected: list[tuple[str, dict[str, Any]]] = []
        verified: dict[str, int] = {}
        inconclusive: list[tuple[str, dict[str, Any]]] = []
        prompt_tokens_spent = 0

        for model in models:
            per_model: dict[str, Any] = {}
            passed: list[int] = []
            for depth in sizes:
                if ctx.out_of_budget():
                    self._note_budget(
                        result,
                        ctx,
                        f"上下文探针已完成 {len(matrix)}/{len(models)} 个模型，"
                        f"停在 {model} 的 {depth} tokens 深度。",
                    )
                    break
                head = _new_code(rng)
                tail = _new_code(rng)
                prompt = _build_prompt(depth, head, tail, rng)
                ctx.progress(
                    f"{model}: 测试约 {depth} tokens 的输入（正文 {len(prompt)} 字符）…"
                )
                try:
                    # ctx.ask, not ctx.client.chat: a reasoning model needs room
                    # to finish thinking before it can echo the markers back, and
                    # an answer that is empty only because of the token budget
                    # must not be read as a truncation.
                    completion = ctx.ask(
                        model,
                        [{"role": "user", "content": prompt}],
                        max_tokens=_MAX_TOKENS,
                        temperature=0,
                    )
                except RelayBudgetExceeded:
                    self._note_budget(
                        result,
                        ctx,
                        f"上下文探针停在 {model} 的 {depth} tokens 深度（超出预算）。",
                    )
                    break
                except RelayError as exc:
                    outcome = _error_outcome(depth, exc)
                except Exception as exc:  # noqa: BLE001 - one depth must not kill the probe
                    outcome = {
                        "depth_tokens": depth,
                        "verdict": "error",
                        "detail": f"{type(exc).__name__}: {exc}"[:200],
                    }
                else:
                    outcome = _classify(completion, head, tail, depth, len(prompt))

                per_model[str(depth)] = outcome
                used = outcome.get("actual_prompt_tokens")
                if isinstance(used, int):
                    prompt_tokens_spent += used

                verdict = outcome["verdict"]
                if verdict == "full":
                    passed.append(depth)
                    continue
                if verdict == "truncated":
                    truncated.append((model, outcome, list(passed)))
                elif verdict == "rejected":
                    rejected.append((model, outcome))
                else:
                    inconclusive.append((model, outcome))
                # A relay that already dropped the head, or already refused the
                # request, will do the same at every larger depth. Climbing further
                # would only cost money to learn nothing.
                break

            if len(per_model) == len(sizes) and len(passed) == len(sizes):
                verified[model] = max(passed) if passed else 0

            matrix[model] = per_model
            result.data.setdefault("models_tested", []).append(model)

        result.data["matrix"] = matrix
        result.data["prompt_tokens_spent"] = prompt_tokens_spent
        result.data["verified_depth_tokens"] = dict(verified)
        result.data["sizes"] = list(sizes)

        self._report(result, sizes=sizes, truncated=truncated, rejected=rejected)
        verified_all = len(verified) == len(models) and bool(models)
        complete = all(len(matrix.get(m, {})) == len(sizes) for m in models)
        if not truncated and not rejected:
            if verified_all and complete:
                best = max(verified.values())
                self._find(
                    result,
                    id="ctx-clean",
                    title="长输入在测试范围内完整到达模型",
                    severity=Severity.CLEAN,
                    confidence=Confidence.CONFIRMED,
                    summary=(
                        f"{len(models)} 个模型在约 {best} tokens 的输入下都取回了文首与文末"
                        "两个标记，说明该深度以内输入没有被静默截断。"
                        "注意这只是一个下限：本次没有测试更深的输入，"
                        "不代表该站承诺的上限一定成立。"
                    ),
                    evidence={
                        "models_verified": sorted(verified),
                        "verified_depth_tokens": verified,
                        "sizes_tested": list(sizes),
                    },
                )
            else:
                self._find(
                    result,
                    id="ctx-000" if not verified else "ctx-102",
                    title=(
                        "长输入是否被截断未能检验"
                        if not verified
                        else "长输入截断检查只完成了一部分"
                    ),
                    severity=Severity.INFO,
                    confidence=Confidence.CONFIRMED,
                    summary=(
                        "本次没有拿到任何可判读的长输入结果"
                        "（请求失败、回答被输出上限截断，或两个标记都没取回），"
                        "因此**这不代表输入是完整的**，只代表这一项没有被检验。"
                        if not verified
                        else
                        "部分深度已经验证完整，但还有深度未能判读；"
                        "已完成的深度结论有效，未完成的部分没有结论。"
                    ),
                    evidence={
                        "sizes_requested": list(sizes),
                        "matrix": matrix,
                        "verified_depth_tokens": verified,
                    },
                    remediation=(
                        "先用 --probes reliability 确认中转站能不能正常服务，"
                        "再重跑 --probes context；若回答被输出上限截断，请调大 --timeout 或稍后重试。"
                    ),
                )
        return result

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _sizes(ctx: ProbeContext) -> list[int]:
        raw = ctx.option("context_sizes")
        if raw is None:
            return list(DEFAULT_SIZES)
        if isinstance(raw, (int, float)):
            candidates: Sequence[Any] = [raw]
        else:
            candidates = re.split(r"[,\s]+", str(raw).strip())
        sizes: list[int] = []
        for item in candidates:
            try:
                value = int(float(item))
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in sizes:
                sizes.append(value)
        if not sizes:
            return list(DEFAULT_SIZES)
        return sorted(sizes)

    def _report(
        self,
        result: ProbeResult,
        *,
        sizes: Sequence[int],
        truncated: list[tuple[str, dict[str, Any], list[int]]],
        rejected: list[tuple[str, dict[str, Any]]],
    ) -> None:
        if truncated:
            self._report_truncation(result, sizes=sizes, truncated=truncated)
            return
        if rejected:
            self._report_rejection(result, rejected)

    def _report_truncation(
        self,
        result: ProbeResult,
        *,
        sizes: Sequence[int],
        truncated: list[tuple[str, dict[str, Any], list[int]]],
    ) -> None:
        blocks = []
        for model, outcome, passed in truncated:
            missing = outcome.get("missing")
            block = {
                "model": model,
                "depth_tokens_requested": outcome["depth_tokens"],
                "actual_prompt_tokens": outcome.get("actual_prompt_tokens"),
                "chars_sent": outcome.get("chars_sent"),
                "missing": missing,
                # ``missing`` is which marker did not come back; ``surviving_side``
                # is the one that did. Both are recorded because the summary text
                # has to name the side that survived, and mixing the two up turns
                # the finding into its own opposite.
                "truncation_direction": "头部被丢弃" if missing == "head" else "尾部被丢弃",
                "surviving_side": "文末" if missing == "head" else "文首",
                "head_code": outcome.get("head_code"),
                "tail_code": outcome.get("tail_code"),
                "answer": outcome.get("answer"),
                "depths_that_passed": passed,
            }
            if passed:
                block["passed_at_tokens"] = max(passed)
                block["failed_at_tokens"] = outcome.get("actual_prompt_tokens") or outcome[
                    "depth_tokens"
                ]
            blocks.append(block)

        # With a passing shallow depth the direction is unambiguous: the same model
        # returned both codes from a shorter document and only the tail from a
        # longer one. Without it, the model may simply have listed one of the two,
        # so the claim is downgraded rather than dropped — the evidence is still
        # there for a human to read.
        strong = [b for b in blocks if b.get("passed_at_tokens")]

        if strong:
            severity = Severity.HIGH
            confidence = Confidence.LIKELY
            lead = strong[0]
            detail = (
                f"{lead['model']} 在约 {lead['depth_tokens_requested']} tokens 的输入下"
                f"只取回了{lead['surviving_side']}的标记：文首标记 "
                f"{lead['head_code']} 没有出现，文末标记 {lead['tail_code']} 出现了；"
                f"而更短的 {lead['passed_at_tokens']} tokens 时两个标记都完整取回。"
                "两个标记都是随机生成的十二位字符串，模型不可能凭空猜出其中一个"
                "却漏掉另一个。"
            )
            summary = (
                detail
                + "这说明**前段输入在到达模型之前就被丢掉了**，而计费仍按你发送的完整输入计算。"
                "被截断的输入不会报错、不会警告，回答照样通顺，所以用户通常察觉不到。"
            )
        else:
            severity = Severity.MEDIUM
            confidence = Confidence.SUSPECTED
            lead = blocks[0]
            detail = (
                f"在最初一档深度（约 {lead['depth_tokens_requested']} tokens）"
                f"{lead['model']} 就只取回了{lead['surviving_side']}的标记："
                f"文首标记 {lead['head_code']} 未出现，文末标记 {lead['tail_code']} 出现了。"
            )
            summary = (
                detail
                + "因为没有任何一档更浅的深度作为对照，也可能是模型只列出了一个标记。"
                "请用更浅的起点重跑一次以确认：`--probes context --context-sizes 500,2000,8000`。"
                "若浅档能取回两个标记、深档只能取回一个，那就是确定的静默截断。"
            )

        self._find(
            result,
            id="ctx-100",
            title="长输入被静默截断：模型没有看到你发送的全部内容",
            severity=severity,
            confidence=confidence,
            summary=summary,
            evidence={
                "cases": blocks,
                "sizes_tested": list(sizes),
                "note": (
                    "标记是每次请求随机生成的十二位字符串，写在正文的最前面和最后面。"
                    "取回哪一个，就证明哪一段输入真的到达了模型。"
                ),
            },
            remediation=(
                "要求商家提供该次请求的上游调用日志，与你自己发送的正文长度对账。"
                "把 prompt_tokens（服务端回报的）与你实际发送的正文规模对比："
                "若服务端只按截断后的内容计费，至少账单是诚实的；"
                "若它按完整输入计费、而模型只看到了一部分，那就是收了钱没给货。"
                "另外可以用 --context-sizes 缩小测试范围复现，确认截断发生在哪一档之间。"
            ),
        )

    def _report_rejection(
        self, result: ProbeResult, rejected: list[tuple[str, dict[str, Any]]]
    ) -> None:
        cases = [
            {
                "model": model,
                "depth_tokens_requested": outcome["depth_tokens"],
                "http_status": outcome.get("status"),
                "detail": outcome.get("detail"),
            }
            for model, outcome in rejected
        ]
        self._find(
            result,
            id="ctx-101",
            title="超出某个长度的请求被直接拒绝（而不是被截断）",
            severity=Severity.INFO,
            confidence=Confidence.CONFIRMED,
            summary=(
                "中转站在某个输入长度上直接返回了错误，而不是悄悄丢掉一部分。"
                "**拒绝比截断诚实**：你至少知道自己没有拿到想要的东西。"
                "这条只作为事实记录——请对照商家宣称的上下文长度自行判断是否名不副实，"
                "本次并没有测试出精确的上限。"
            ),
            evidence={"cases": cases},
            remediation=(
                "把这条和商家标注的上下文长度对比。若宣称 128k 而在此处就被拒绝，"
                "请向商家索取该模型真实的上下文上限，并要求按真实能力定价。"
            ),
        )


# ------------------------------------------------------------------ prompt build


def _new_code(rng: random.Random) -> str:
    return "".join(rng.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))


def _build_prompt(
    target_tokens: int, head: str, tail: str, rng: random.Random
) -> str:
    """One long document with the head code first and the tail code last.

    Both codes sit in bracketed labels so a model can name them even if it
    reformats the answer, and the question is the very last line so a relay that
    keeps the *first* N tokens drops it entirely.
    """
    target_chars = max(1200, int(target_tokens * _CHARS_PER_TOKEN))
    lines = [
        _OPENING,
        f"[{_HEAD_LABEL}-{head}]",
    ]
    filler = list(_FILLER)
    rng.shuffle(filler)
    size = 0
    index = 0
    while size < target_chars:
        # Re-shuffle on every pass so a very long document is not one sentence
        # repeated four hundred times; the tokenizer count should stay realistic.
        if index >= len(filler):
            rng.shuffle(filler)
            index = 0
        sentence = filler[index]
        index += 1
        lines.append(sentence)
        size += len(sentence) + 1
    lines.append(f"[{_TAIL_LABEL}-{tail}]")
    lines.append(_QUESTION)
    return "\n".join(lines)


# ------------------------------------------------------------------- classify

#: Ways a long-context model says "I could not find that marker". Such a reply
#: has to be read as *talk about* the markers rather than as a list of the ones
#: it received: the same sentence names the code it did see and the other one as
#: absent, and a bare substring test reads that as "the head survived but the
#: tail did not" — i.e. as proof that the front of the input was dropped before
#: it ever reached the model. It is proof of nothing; it is a refusal.
_MARKER_DENIAL_RE = re.compile(
    r"CANNOT|CAN NOT|CAN'T|DO NOT SEE|DON'T SEE|NOT SEE|NOT FIND|UNABLE"
    r"|MISSING|ABSENT|ONLY ONE|ONLY SEE|JUST ONE"
    r"|只看到|只有一个|只出现|没看到|没有看到|看不到|无法|未见",
)


def _classify(
    completion: Completion, head: str, tail: str, depth: int, chars_sent: int
) -> dict[str, Any]:
    text = (completion.content or "").upper()
    saw_head = head in text
    saw_tail = tail in text
    outcome: dict[str, Any] = {
        "depth_tokens": depth,
        "actual_prompt_tokens": completion.usage.prompt_tokens,
        "chars_sent": chars_sent,
        "finish_reason": completion.finish_reason,
        "answer_chars": completion.visible_chars,
        "saw_head": saw_head,
        "saw_tail": saw_tail,
        "head_code": head,
        "tail_code": tail,
        "answer": (completion.content or "")[:200],
    }

    if saw_head and saw_tail:
        outcome["verdict"] = "full"
        return outcome

    if completion.finish_reason == "length":
        # The model was cut off before it finished listing. We cannot tell a
        # dropped code from an unfinished answer, so this depth proves nothing.
        outcome["verdict"] = "cut_short"
        return outcome

    if _MARKER_DENIAL_RE.search(text):
        # The reply is *talking about* the markers, not listing them. In that
        # register a code quoted once cannot be told apart from a code named as
        # missing, and the one-sided reading becomes "前段输入在到达模型之前就被
        # 丢掉了，而计费仍按完整输入计算" — a HIGH-severity accusation built on a
        # polite refusal. This depth proves nothing, and it must say so.
        outcome["verdict"] = "denied"
        return outcome

    if saw_head or saw_tail:
        outcome["verdict"] = "truncated"
        outcome["missing"] = "tail" if saw_head else "head"
        return outcome

    outcome["verdict"] = "neither"
    return outcome


def _error_outcome(depth: int, exc: RelayError) -> dict[str, Any]:
    status = exc.status
    detail = exc.describe(200)
    size_rejection = status in _SIZE_REJECT_STATUSES or (
        status == 400 and bool(_SIZE_REJECT_HINT.search(detail))
    )
    return {
        "depth_tokens": depth,
        "verdict": "rejected" if size_rejection else "error",
        "status": status,
        "detail": detail,
        "size_rejection": size_rejection,
    }
