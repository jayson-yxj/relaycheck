"""Streaming integrity.

Streaming is where a relay is easiest to fake, because almost nobody checks it.
Four things we verify:

1. **Same answer.** The streamed text must match the non-streamed text for the
   same prompt. A relay that streams a short canned answer while billing the
   full completion is caught here.
2. **The comparison is even possible.** Text equality is only evidence when the
   two calls were sampled the same way *and* the station reproduces itself. So
   both paths are sent identical sampling parameters (``temperature=0`` and the
   same ``max_tokens``), and the non-streaming call is repeated as a control.
   If those two identical requests come back different, the station has no
   reproducible output at all and a stream/plain difference proves nothing —
   that is reported as unverifiable, never as a mismatch. Comparing a
   ``temperature=0`` call against a default-temperature call, or one sample
   against another sample, produces a HIGH-severity accusation out of ordinary
   sampling noise; that was a real false positive against an honest relay.
3. **Usage is reported.** With ``stream_options.include_usage=true`` the final
   chunk must carry ``usage``. Without it, streamed calls are unauditable —
   you cannot reconstruct what you were billed.
4. **It actually streams.** If every chunk arrives in one burst at the very end,
   the "stream" is a pre-generated or buffered response. Not fraud per se, but
   it breaks TTFT expectations and is worth reporting.

When the open-ended comparison is unusable (case 2), the probe falls back to a
question with exactly one correct answer. Sampling noise cannot change the
answer to ``37 * 41``, so if the two paths disagree *there*, one of them is not
the model being billed. If they agree, the check still proves less than the
open-ended one, and the report says so.
"""

from __future__ import annotations

import re
from typing import Any

from ..client import RelayBudgetExceeded, RelayError
from ..models import Confidence, ProbeResult, Severity
from .base import Probe, ProbeContext

_PROMPT = (
    "Write a short paragraph of about 80 words explaining what a TCP handshake is. "
    "Plain text only."
)

#: If all chunks land inside this fraction of the total duration, treat the
#: stream as buffered rather than incremental.
_BURST_FRACTION = 0.15

#: Cap for both paths. Generous on purpose: a reasoning model spends hundreds of
#: tokens on hidden ``reasoning_content`` before emitting any visible text, and
#: an empty answer on *both* paths makes the comparison meaningless. This is a
#: ceiling, not a reservation — a model that answers in 120 tokens still costs
#: 120 tokens.
_MAX_TOKENS = 1200

#: Deterministic fallback, used only when the station cannot reproduce itself on
#: the open-ended prompt. One question, one correct answer: no amount of
#: sampling noise turns 1517 into something else, so a difference between the
#: two paths here is a real difference between the two backends. The number is
#: deliberately odd and multi-digit — a canned or cached answer would not
#: contain it by accident.
_FACT_PROMPT = "What is 37 * 41? Reply with the number only."
_FACT_ANSWER = "1517"
_FACT_MAX_TOKENS = 1024


class StreamProbe(Probe):
    name = "stream"
    description = "流式返回与完整返回是否同一份内容，以及流里有没有 usage"

    DEFAULT_MAX_MODELS = 3

    def run(self, ctx: ProbeContext) -> ProbeResult:
        result = self._result()
        max_models = int(ctx.option("stream_max_models", self.DEFAULT_MAX_MODELS))
        models = list(ctx.models)[:max_models]

        observations: dict[str, Any] = {}
        mismatches: list[dict[str, Any]] = []
        fact_mismatches: list[dict[str, Any]] = []
        empties: list[dict[str, Any]] = []
        unverifiable: list[dict[str, Any]] = []
        no_usage: list[dict[str, Any]] = []
        bursty: list[dict[str, Any]] = []

        for model in models:
            if ctx.out_of_budget():
                self._note_budget(
                    result, ctx, f"已完成 {len(observations)}/{len(models)} 个模型的流式比对。"
                )
                break
            messages = [{"role": "user", "content": _PROMPT}]
            try:
                # Identical sampling parameters on both paths, on purpose. A
                # streamed request at the default temperature and a plain request
                # at temperature=0 are two different experiments; diffing their
                # outputs measures the parameter difference, not the relay.
                streamed = ctx.client.chat_stream(
                    model, messages, max_tokens=_MAX_TOKENS, temperature=0
                )
                plain = ctx.client.chat(
                    model, messages, max_tokens=_MAX_TOKENS, temperature=0
                )
                # Control: the same request, twice. Whatever this pair does is
                # the station's own noise floor, and it bounds every conclusion
                # below.
                control = ctx.client.chat(
                    model, messages, max_tokens=_MAX_TOKENS, temperature=0
                )
            except RelayBudgetExceeded:
                self._note_budget(
                    result,
                    ctx,
                    f"比对 {model} 的流式输出时超出预算"
                    f"（已完成 {len(observations)}/{len(models)} 个模型）。",
                )
                break
            except RelayError as exc:
                observations[model] = {"error": exc.describe(140)}
                continue

            gaps = streamed.gaps()
            total = streamed.total_s or 0.0
            late = sum(g for g in gaps if g >= 0)
            baseline = _compare_answers(plain.content, control.content)
            record: dict[str, Any] = {
                "chunks": streamed.chunk_count,
                "ttft_s": round(streamed.ttft_s, 3) if streamed.ttft_s is not None else None,
                "total_s": round(total, 3),
                "streamed_chars": len(streamed.text),
                "plain_chars": len(plain.content),
                "control_chars": len(control.content),
                "baseline": baseline,
                "usage_in_stream": streamed.usage.to_dict() if streamed.usage else None,
                "model_returned": streamed.model_returned,
                "finish_reason": streamed.finish_reason,
                "plain_finish_reason": plain.finish_reason,
            }
            observations[model] = record

            if baseline == "match":
                # The station reproduces itself, so a text difference between the
                # two paths means something.
                verdict = _compare_answers(streamed.text, plain.content)
                record["verdict"] = verdict
                record["basis"] = "open-ended"
                if verdict == "mismatch":
                    mismatches.append({
                        "model": model,
                        "streamed_sample": streamed.text[:200],
                        "plain_sample": plain.content[:200],
                        "streamed_chars": len(streamed.text),
                        "plain_chars": len(plain.content),
                    })
                elif verdict == "empty":
                    empties.append(_empty_record(model, streamed, plain))
            else:
                # Two identical requests came back different (or empty). Any
                # stream-vs-plain difference is then just more of the same noise,
                # so the open-ended comparison is discarded and we ask a question
                # that has only one possible answer.
                record["basis"] = "deterministic-fallback"
                record["verdict"] = "noisy"
                try:
                    fact_messages = [{"role": "user", "content": _FACT_PROMPT}]
                    fact_stream = ctx.client.chat_stream(
                        model, fact_messages, max_tokens=_FACT_MAX_TOKENS, temperature=0
                    )
                    fact_plain = ctx.client.chat(
                        model, fact_messages, max_tokens=_FACT_MAX_TOKENS, temperature=0
                    )
                except RelayBudgetExceeded:
                    self._note_budget(
                        result, ctx, f"对 {model} 做确定性问答回退时超出预算。"
                    )
                    break
                except RelayError as exc:
                    record["fallback_error"] = exc.describe(140)
                else:
                    fact = _compare_answers(fact_stream.text, fact_plain.content)
                    answered = _FACT_ANSWER in fact_stream.text and _FACT_ANSWER in fact_plain.content
                    record["fallback"] = {
                        "prompt": _FACT_PROMPT,
                        "streamed_sample": fact_stream.text[:120],
                        "plain_sample": fact_plain.content[:120],
                        "verdict": fact,
                        "reference_answer_present": answered,
                    }
                    if fact == "mismatch" and answered:
                        # One question, one correct answer, two paths, two
                        # answers. Sampling cannot explain this.
                        fact_mismatches.append({
                            "model": model,
                            "streamed_sample": fact_stream.text[:200],
                            "plain_sample": fact_plain.content[:200],
                            "prompt": _FACT_PROMPT,
                        })
                    elif fact == "match" and answered:
                        record["verdict"] = "noisy_but_agreed"
                    else:
                        # The paths did not disagree on a question with one
                        # answer, but we also did not get that answer back, so
                        # nothing was verified.
                        unverifiable.append({
                            "model": model,
                            "reason": "确定性问答未得到可判定的答案",
                            "open_ended_baseline": baseline,
                            "fallback": record["fallback"],
                        })

            if record.get("verdict") == "noisy":
                unverifiable.append({
                    "model": model,
                    "reason": "两次相同请求（temperature=0）返回不一致，开放式比对不可用",
                    "open_ended_baseline": baseline,
                    "plain_sample": plain.content[:120],
                    "control_sample": control.content[:120],
                })
            elif record.get("verdict") == "noisy_but_agreed":
                # The station has no reproducible output, so the open-ended
                # comparison was discarded — but the deterministic fallback
                # agreed on both paths. That is a *weaker* check that passed, not
                # the real check passing. It has to land in the same bucket as
                # the other unreproducible cases, otherwise the report falls
                # through to "流式与完整返回内容一致" and claims a comparison that
                # by construction never ran.
                unverifiable.append({
                    "model": model,
                    "reason": (
                        "两次相同请求（temperature=0）返回不一致，开放式比对不可用；"
                        "确定性问答回退的两条路径一致"
                    ),
                    "open_ended_baseline": baseline,
                    "plain_sample": plain.content[:120],
                    "control_sample": control.content[:120],
                    "fallback": record.get("fallback"),
                })

            if not streamed.usage or streamed.usage.total_tokens is None:
                no_usage.append({"model": model, "chunks": streamed.chunk_count})

            if streamed.chunk_count > 3 and total > 0 and late / total < _BURST_FRACTION:
                bursty.append({
                    "model": model,
                    "chunks": streamed.chunk_count,
                    "stream_span_s": round(late, 3),
                    "total_s": round(total, 3),
                })

        result.data["observations"] = observations

        if mismatches or fact_mismatches:
            # Two different strengths of evidence, kept apart in the report.
            # `mismatches` is a text difference on an open-ended prompt from a
            # station that reproduces itself. `fact_mismatches` is a difference
            # on a question with exactly one answer, from a station that does
            # not. The second is the stronger of the two.
            parts: list[str] = []
            if fact_mismatches:
                parts.append(
                    f"有 {len(fact_mismatches)} 个模型在同一个确定性问答"
                    f"（{_FACT_PROMPT}）上，流式与完整返回给出了不同答案"
                    "——同一问题只有一个正确答案，采样噪声无法解释这种差异。"
                )
            if mismatches:
                worst = mismatches[0]
                parts.append(
                    f"另有 {len(mismatches)} 个模型在同一开放式提示词下，"
                    "流式与完整模式的回答不同"
                    f"（例如 {worst['model']}：流式 {worst['streamed_chars']} 字符 vs "
                    f"完整 {worst['plain_chars']} 字符）；该站在 temperature=0 下可复现，"
                    "因此这不是采样噪声。"
                )
            parts.append(
                "这意味着其中一条路径返回的不是同一个模型的输出，"
                "或者两条路径的计费口径不同。"
            )
            self._find(
                result,
                id="stream-100",
                title="流式返回与完整返回的内容不一致",
                severity=Severity.HIGH,
                confidence=Confidence.LIKELY,
                summary="".join(parts),
                evidence={
                    "deterministic_prompt_mismatches": fact_mismatches,
                    "open_ended_mismatches": mismatches,
                    "observations": observations,
                },
                remediation="要求中转站说明两条路径的上游，并出示两者的原始 usage。",
            )
        else:
            compared = [o for o in observations.values() if "verdict" in o]
            confirmed = [o for o in compared if o.get("verdict") == "match"]
            if not compared:
                # Every comparison failed at the HTTP layer. Saying "stream and
                # non-stream agree" here would be reporting a check that never ran.
                self._find(
                    result,
                    id="stream-000",
                    title="流式比对未能完成",
                    severity=Severity.INFO,
                    confidence=Confidence.CONFIRMED,
                    summary=(
                        f"{len(models)} 个模型的流式/完整比对都没有成功返回，"
                        "因此无法判断两条路径是否一致。这一项没有被检验。"
                    ),
                    evidence={"observations": observations},
                )
            elif empties:
                # Nothing to compare. This is the reasoning-model case: the whole
                # max_tokens budget went into hidden reasoning, so both paths
                # returned an empty body. Emitting stream-clean here would claim
                # a comparison that never happened.
                result.data["partial"] = True
                self._find(
                    result,
                    id="stream-103",
                    title="流式比对因返回为空而无法判定",
                    severity=Severity.INFO,
                    confidence=Confidence.CONFIRMED,
                    summary=(
                        f"有 {len(empties)} 个模型的流式与完整返回都没有可见正文，"
                        "没有可比对的内容，因此无法判断两条路径是否一致。"
                        "常见原因是推理模型把 max_tokens 全花在了隐藏的 reasoning 上，"
                        "正文还没开始生成。这一项没有被检验——"
                        "**返回为空不等于两条路径不一致**。"
                    ),
                    evidence={
                        "models_inconclusive": empties,
                        "models_compared": len(confirmed),
                        "observations": observations,
                    },
                    remediation=(
                        "确认该模型是否为推理模型；是的话请调大 max_tokens 后重跑本探针，"
                        "并把 reasoning_content 与正文一并纳入核对。"
                    ),
                )
            elif unverifiable:
                # The station does not reproduce itself, so the open-ended diff is
                # unusable by construction. Chasing the fallback is worth doing
                # (it can still catch a genuine split), but surviving it is not
                # the same as passing the real comparison, and the report must
                # not dress one up as the other.
                result.data["partial"] = True
                agreed = [
                    u for u in unverifiable
                    if u.get("fallback", {}).get("verdict") == "match"
                    and u.get("fallback", {}).get("reference_answer_present")
                ]
                summary = (
                    f"有 {len(unverifiable)} 个模型在两次完全相同的请求"
                    "（temperature=0、同一提示词）下返回了不同的内容，"
                    "即该站本身不提供可复现的输出。这种情况下"
                    "「流式与完整回答不同」是采样噪声的预期表现，不能作为掉包证据，"
                    "因此本探针没有据此指控。"
                )
                if agreed:
                    summary += (
                        f"已改用确定性问答回退，其中 {len(agreed)} 个模型的两条路径"
                        "给出了同一个正确答案，即未发现两条路径的实质分歧；"
                        "但这一项只覆盖一个问题，强度低于开放式比对。"
                    )
                else:
                    summary += "确定性问答回退也未能得到可判定的答案，两条路径是否一致**没有被检验**。"
                self._find(
                    result,
                    id="stream-104",
                    title="该站输出不可复现，流式比对无法判定",
                    severity=Severity.INFO,
                    confidence=Confidence.CONFIRMED,
                    summary=summary,
                    evidence={
                        "unverifiable": unverifiable,
                        "models_compared": len(confirmed),
                        "observations": observations,
                    },
                    remediation=(
                        "该站大概率没有把 temperature=0 透传到上游，或上游后端本身非确定性。"
                        "两种原因本探针无法区分；如需判定，请在客户端对比同一提示词的多次输出。"
                    ),
                )
            else:
                self._find(
                    result,
                    id="stream-clean",
                    title="流式与完整返回内容一致",
                    severity=Severity.CLEAN,
                    confidence=Confidence.CONFIRMED,
                    summary="同一提示词下，流式与完整模式返回的文本内容一致。",
                    evidence={
                        "models_compared": len(confirmed),
                        "observations": observations,
                    },
                )

        if no_usage:
            self._find(
                result,
                id="stream-101",
                title="流式响应不返回 usage，导致无法对账",
                severity=Severity.MEDIUM,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"有 {len(no_usage)} 个模型在请求 include_usage 后仍未在流中返回 usage。"
                    "用户因此无法核对流式请求被计费了多少 token，"
                    "这为虚报用量提供了空间。"
                ),
                evidence={"models_without_stream_usage": no_usage},
                remediation="标准做法是在最后一个 chunk 返回 usage；缺失属于兼容性缺陷。",
            )

        if bursty:
            self._find(
                result,
                id="stream-102",
                title="流式响应疑似一次性下发（非真实增量生成）",
                severity=Severity.LOW,
                confidence=Confidence.SUSPECTED,
                summary=(
                    f"有 {len(bursty)} 个模型的全部 chunk 在总耗时的一小部分内集中到达，"
                    "不符合逐 token 生成的时序特征，可能是先取完整结果再切片下发。"
                    "这不必然代表造假，但会让首字延迟指标失真。"
                ),
                evidence={"bursty": bursty},
            )

        return result


def _empty_record(model: str, streamed: Any, plain: Any) -> dict[str, Any]:
    """A model whose streamed and plain paths both came back without visible text."""
    return {
        "model": model,
        "streamed_chars": len(streamed.text),
        "plain_chars": len(plain.content),
        "stream_finish_reason": streamed.finish_reason,
        "plain_finish_reason": plain.finish_reason,
        "stream_reasoning_tokens": (
            streamed.usage.reasoning_tokens if streamed.usage else None
        ),
        "plain_reasoning_tokens": plain.reasoning_tokens,
        "stream_chunks": streamed.chunk_count,
    }


def _compare_answers(a: str, b: str) -> str:
    """Compare streamed and non-streamed text.

    Returns ``"match"``, ``"mismatch"`` or ``"empty"``.

    The ``"empty"`` state is the whole point. Two empty answers are not two
    *different* answers — they are two answers that were never produced. A
    reasoning model whose entire ``max_tokens`` budget went into hidden
    ``reasoning_content`` returns ``content: ""`` on both paths, and treating
    that as a mismatch produces a HIGH-severity accusation against a relay that
    did nothing wrong. "We could not compare" and "the two paths disagree" must
    never collapse into the same verdict.

    ``"mismatch"`` carries no weight on its own either: it means *these two
    texts differ*, and on a station that does not reproduce its own output at
    ``temperature=0`` two samples of the same model differ all the time. Callers
    must establish reproducibility (see ``StreamProbe.run``) before treating a
    mismatch as evidence.
    """
    na = re.sub(r"\s+", " ", a).strip()
    nb = re.sub(r"\s+", " ", b).strip()
    if not na or not nb:
        return "empty"
    if na == nb:
        return "match"
    # Tolerate a small tail difference (stream truncation at finish).
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) >= 0.9 * len(longer) and longer.startswith(shorter[: len(shorter)]):
        return "match"
    return "mismatch"
