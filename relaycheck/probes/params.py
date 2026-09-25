"""Parameter compliance.

A relay that accepts a parameter and silently ignores it is selling you an API
you are not getting. The consequences are not cosmetic:

* ignoring ``max_tokens`` means you are billed for output you did not ask for;
* ignoring ``temperature`` means every request behaves like a slot machine;
* ignoring ``stop`` breaks agent loops and structured pipelines;
* a fake ``logprobs`` field breaks downstream tooling that trusts it.

Each check is a *behavioural* test — we do not trust a 200 status, we look at
what the response actually does.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..client import RelayBudgetExceeded, RelayError
from ..models import Confidence, ProbeResult, Severity
from .base import (
    Probe,
    ProbeContext,
    grow_budget,
    starved_of_visible_text,
)

_COUNT_PROMPT = (
    "Count from 1 to 400, one number per line, nothing else before or after."
)
_STOP_PROMPT = (
    "Write the digits 0 through 9 in order, separated by single spaces. "
    "Output nothing else."
)
_STOP_TOKEN = "5"
_SAMPLING_PROMPT = (
    "Write one original sentence of 15 to 30 words describing an imaginary city. "
    "Output only the sentence."
)
_JSON_PROMPT = 'Return a JSON object with keys "a" (integer 1) and "b" (string "two").'
_TOOL_MESSAGE = "What is the weather in Reykjavik right now? Use the tool."

_TOOL_SCHEMA = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}]


class ParamsProbe(Probe):
    name = "params"
    description = "验证中转站是否真的透传它接受的那些 OpenAI 参数"

    DEFAULT_MAX_MODELS = 2

    def run(self, ctx: ProbeContext) -> ProbeResult:
        result = self._result()
        max_models = int(ctx.option("params_max_models", self.DEFAULT_MAX_MODELS))
        models = list(ctx.models)[:max_models]

        only = ctx.option("params_checks")
        checks: list[tuple[str, Callable[[ProbeContext, str], dict[str, Any]]]] = [
            ("max_tokens", self._check_max_tokens),
            ("stop", self._check_stop),
            ("temperature", self._check_temperature),
            ("n", self._check_n),
            ("response_format", self._check_response_format),
            ("logprobs", self._check_logprobs),
            ("tools", self._check_tools),
        ]
        if only:
            wanted = {str(x).strip() for x in str(only).split(",")}
            checks = [c for c in checks if c[0] in wanted]

        matrix: dict[str, dict[str, Any]] = {}
        ignored: list[dict[str, Any]] = []
        unsupported: list[dict[str, Any]] = []
        errored: list[dict[str, Any]] = []
        inconclusive: list[dict[str, Any]] = []
        nondeterministic: list[dict[str, Any]] = []

        for model in models:
            if ctx.out_of_budget():
                self._note_budget(
                    result, ctx, f"已完成 {len(matrix)}/{len(models)} 个模型的参数检查。"
                )
                break
            per_model: dict[str, Any] = {}
            for check_name, fn in checks:
                if ctx.out_of_budget():
                    self._note_budget(
                        result,
                        ctx,
                        f"检查 {model} 的 {check_name} 时超出预算"
                        f"（已完成 {len(matrix)}/{len(models)} 个模型）。",
                    )
                    break
                try:
                    outcome = fn(ctx, model)
                except RelayBudgetExceeded:
                    self._note_budget(
                        result,
                        ctx,
                        f"检查 {model} 的 {check_name} 时超出预算"
                        f"（已完成 {len(matrix)}/{len(models)} 个模型）。",
                    )
                    break
                except RelayError as exc:
                    outcome = {
                        "verdict": "error",
                        "status": exc.status,
                        "detail": str(exc)[:160],
                    }
                except Exception as exc:  # noqa: BLE001 - one check must not kill the probe
                    outcome = {"verdict": "error", "detail": f"{type(exc).__name__}: {exc}"[:160]}
                per_model[check_name] = outcome
                if outcome.get("verdict") == "ignored":
                    ignored.append({"model": model, "param": check_name, **outcome})
                elif outcome.get("verdict") == "unsupported":
                    unsupported.append({"model": model, "param": check_name, **outcome})
                elif outcome.get("verdict") == "error":
                    errored.append({"model": model, "param": check_name, **outcome})
                elif outcome.get("verdict") == "inconclusive":
                    # The check ran but had nothing to judge. Counting this as a
                    # pass would let "we could not tell" masquerade as "the
                    # parameter works"; counting it as a failure would accuse an
                    # honest relay. It is neither, so it gets its own bucket.
                    inconclusive.append({"model": model, "param": check_name, **outcome})
                elif outcome.get("verdict") == "nondeterministic":
                    # The check produced an observation we cannot act on: the
                    # station does not reproduce itself, which is consistent with
                    # both a dropped parameter and a non-deterministic backend.
                    # It must not reach `ignored`, which is CONFIRMED and reads
                    # as an accusation.
                    nondeterministic.append({"model": model, "param": check_name, **outcome})
            matrix[model] = per_model
            if ctx.out_of_budget():
                break

        result.data["matrix"] = matrix

        if errored:
            # Never let a crashed check masquerade as a passing one.
            self._find(
                result,
                id="params-102",
                title="部分参数检查未能执行",
                severity=Severity.INFO,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"有 {len(errored)} 项参数检查因请求失败而无法得出结论。"
                    "这些参数未经过验证，不要把它们当作已通过。"
                ),
                evidence={"errored": errored},
            )

        if inconclusive:
            # Same rule as params-102, different cause: the check ran and got a
            # reply, but the reply was too short (or empty) to judge. A reasoning
            # model whose whole max_tokens budget went into hidden reasoning
            # lands here — and reporting that as "the parameter was ignored"
            # would be a false accusation.
            self._find(
                result,
                id="params-103",
                title="部分参数检查因样本不可用而无法判定",
                severity=Severity.INFO,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"有 {len(inconclusive)} 项参数检查拿到了回复，但回复为空或过短，"
                    "没有可比较的内容，无法判断参数是否生效。这一项没有被检验——"
                    "**空回复不等于参数被忽略**。"
                ),
                evidence={"inconclusive": inconclusive},
                remediation="对被测模型调大 max_tokens 后重跑本探针。",
            )

        if nondeterministic:
            # Reported separately from params-100 on purpose. "The station does
            # not reproduce its own output" is a fact worth knowing, but it is
            # not the same fact as "the parameter was silently dropped" — the
            # first is also what a perfectly honest MoE backend looks like.
            #
            # INFO, not LOW: what is confirmed here is the observation (two
            # identical requests differed). The reading that would make it a
            # defect — the parameter never reached the upstream — is exactly the
            # reading this probe cannot distinguish, and the summary says so. A
            # severity that contradicts its own finding text is how an honest
            # station ends up in the "问题" column of the report.
            self._find(
                result,
                id="params-104",
                title="该站输出不可复现，无法验证采样参数",
                severity=Severity.INFO,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"有 {len(nondeterministic)} 项采样参数检查中，temperature=0 的两次"
                    "相同请求返回了不同内容。这可能是中转站丢弃了 temperature，"
                    "也可能是上游后端本身非确定性（MoE 路由、批处理等），"
                    "本探针无法区分——因此**本条不指控参数被忽略**。"
                ),
                evidence={"nondeterministic": nondeterministic},
                remediation=(
                    "客户端侧对同一提示词连发多次、比较输出是否一致即可自行确认；"
                    "若你需要可复现的输出（评测、回归测试），该站不适合。"
                ),
            )

        if ignored:
            by_param: dict[str, int] = {}
            for item in ignored:
                by_param[item["param"]] = by_param.get(item["param"], 0) + 1
            summary = "、".join(f"{k}({v})" for k, v in sorted(by_param.items()))
            self._find(
                result,
                id="params-100",
                title=f"中转站接受但忽略部分参数：{summary}",
                severity=Severity.MEDIUM,
                confidence=Confidence.CONFIRMED,
                summary=(
                    "以下参数被接口正常接收（返回 200），但行为上没有任何效果："
                    f"{summary}。这意味着你传入的控制参数被静默丢弃。"
                    "对 max_tokens 而言，这直接关系到你的账单。"
                ),
                evidence={"ignored": ignored, "counts": by_param},
                remediation=(
                    "要求中转站修复参数透传；"
                    "在修复前，不要把依赖这些参数的自动化流程接到该站。"
                ),
            )
        elif errored or inconclusive or nondeterministic:
            # Some checks never ran, or ran without producing anything judgeable.
            # "No parameter was ignored" would be an overclaim: those parameters
            # were not tested at all. params-102 / params-103 / params-104 already
            # report the gap, and stacking a CLEAN verdict on top of it is exactly
            # the "crashed check masquerading as a passing one" failure this probe
            # exists to avoid. The positive partial result is still in
            # result.data["matrix"].
            result.data["partial"] = True
            result.data["checks_completed"] = (
                sum(len(v) for v in matrix.values())
                - len(errored)
                - len(inconclusive)
                - len(nondeterministic)
            )
        else:
            self._find(
                result,
                id="params-clean",
                title="参数透传正常",
                severity=Severity.CLEAN,
                confidence=Confidence.CONFIRMED,
                summary="所有被测试的参数都产生了预期的行为变化，未发现静默忽略。",
                evidence={"matrix": matrix},
            )

        if unsupported:
            self._find(
                result,
                id="params-101",
                title="部分标准参数不被支持",
                severity=Severity.INFO,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"有 {len(unsupported)} 项标准参数返回错误而非静默忽略。"
                    "这是兼容性缺口而非欺骗行为，但对依赖这些能力的调用方是阻塞问题。"
                ),
                evidence={"unsupported": unsupported},
            )

        return result

    # ------------------------------------------------------------------- checks

    def _check_max_tokens(self, ctx: ProbeContext, model: str) -> dict[str, Any]:
        comp = ctx.client.chat(
            model, [{"role": "user", "content": _COUNT_PROMPT}], max_tokens=16, temperature=0
        )
        billed = comp.usage.completion_tokens
        over = billed is not None and billed > 24
        return {
            "verdict": "ignored" if over else "honoured",
            "requested_max_tokens": 16,
            "billed_completion_tokens": billed,
            "finish_reason": comp.finish_reason,
            "visible_chars": comp.visible_chars,
        }

    def _check_stop(self, ctx: ProbeContext, model: str) -> dict[str, Any]:
        # Differential test. Asking the model to echo a friendly word proves
        # nothing (a canned answer never contains it either). Instead: get the
        # baseline answer, then re-ask with a stop token the answer must contain.
        # An honouring relay truncates at that token; an ignoring relay returns
        # the same full text.
        plain = ctx.ask(
            model, [{"role": "user", "content": _STOP_PROMPT}], max_tokens=64, temperature=0
        ).content
        if _STOP_TOKEN not in plain:
            return {
                "verdict": "inconclusive",
                "stop": [_STOP_TOKEN],
                "note": "基线回答里没有出现该 stop token，无法判定",
                "baseline": plain[:160],
            }
        stopped = ctx.ask(
            model,
            [{"role": "user", "content": _STOP_PROMPT}],
            max_tokens=64,
            temperature=0,
            stop=[_STOP_TOKEN],
        ).content
        honoured = _STOP_TOKEN not in stopped and len(stopped) < len(plain)
        return {
            "verdict": "honoured" if honoured else "ignored",
            "stop": [_STOP_TOKEN],
            "baseline_chars": len(plain),
            "stopped_chars": len(stopped),
            "stopped_response": stopped[:160],
            "note": "带 stop 的返回与不带 stop 完全一致，说明 stop 被静默丢弃",
        }

    def _check_temperature(self, ctx: ProbeContext, model: str) -> dict[str, Any]:
        # Two independent properties have to hold for `temperature` to be real:
        #   1. temperature=0 is reproducible (greedy decoding);
        #   2. a high temperature actually injects variety.
        # Testing only (1) would pass a relay that hard-codes one sampler; testing
        # only (2) would pass a relay that is random but ignores the value.
        a = self._sample(ctx, model, 0.0)
        b = self._sample(ctx, model, 0.0)
        c = self._sample(ctx, model, 1.5)
        d = self._sample(ctx, model, 1.5)

        stable = _same_text(a, b)
        # Named for what it measures: ``hot_identical`` is True when two
        # temperature=1.5 calls came back byte-identical, which is the failure.
        hot_identical = _same_text(c, d)

        if stable is None or hot_identical is None:
            # At least one sample pair had nothing to compare. Reasoning models
            # return an empty ``content`` when their max_tokens budget goes into
            # hidden reasoning, and an empty pair is not a "different" pair —
            # calling it one would blame the relay for our own under-budgeting.
            verdict = "inconclusive"
            note = (
                "四次采样的返回为空或过短，没有可比较的内容，"
                "无法判断 temperature 是否生效。"
                "常见原因是推理模型把 max_tokens 全花在了隐藏的 reasoning 上。"
            )
        elif not stable:
            # Two identical temperature=0 requests came back different. That is
            # *not* proof that the parameter was dropped — a Mixture-of-Experts
            # or aggressively batched backend can be non-deterministic at
            # temperature=0 all by itself, and plenty of relays that do forward
            # the field still land here. Calling this "ignored" is how an honest
            # reasoning relay gets accused of silently discarding a parameter it
            # honours perfectly. It is a real observation, so it gets its own
            # verdict at its own confidence — never the CONFIRMED one.
            verdict = "nondeterministic"
            note = (
                "temperature=0 的两次调用结果不一致，即该站本身不提供可复现输出。"
                "这可能来自中转站丢弃 temperature，也可能来自上游后端本身的非确定性"
                "（MoE 路由、批处理等），本探针无法区分。"
                "**注意：这不是「参数被忽略」的证据。**"
            )
        elif hot_identical:
            verdict = "ignored"
            note = (
                "temperature=1.5 的两次调用结果逐字相同：temperature 被静默丢弃，"
                "每个请求都等价于同一次固定采样。"
            )
        else:
            verdict = "honoured"
            note = "temperature=0 可复现、temperature=1.5 产生差异，采样参数确实生效。"

        return {
            "verdict": verdict,
            "temp0_runs_identical": stable,
            "temp1_5_runs_differ": None if hot_identical is None else not hot_identical,
            "samples": {
                "temp0_a": a[:120],
                "temp0_b": b[:120],
                "temp1_5_a": c[:120],
                "temp1_5_b": d[:120],
            },
            "note": note,
        }

    @staticmethod
    def _sample(ctx: ProbeContext, model: str, temperature: float) -> str:
        # ctx.ask, not ctx.client.chat: a reasoning model needs more than 120
        # tokens before it emits any visible text at all.
        comp = ctx.ask(
            model,
            [{"role": "user", "content": _SAMPLING_PROMPT}],
            max_tokens=120,
            temperature=temperature,
        )
        return comp.content.strip()

    def _check_n(self, ctx: ProbeContext, model: str) -> dict[str, Any]:
        comp_raw = self._raw_chat(ctx, model, _SAMPLING_PROMPT, n=2, max_tokens=48)
        choices = comp_raw.get("choices") if isinstance(comp_raw, dict) else None
        count = len(choices) if isinstance(choices, list) else 0
        return {
            "verdict": "honoured" if count >= 2 else "ignored",
            "requested_n": 2,
            "returned_choices": count,
        }

    def _check_response_format(self, ctx: ProbeContext, model: str) -> dict[str, Any]:
        raw = self._raw_chat(
            ctx,
            model,
            _JSON_PROMPT,
            grow=True,
            max_tokens=120,
            response_format={"type": "json_object"},
        )
        text = _first_content(raw)
        if not text.strip():
            # Nothing was returned, so there is nothing to parse and nothing to
            # accuse. Reporting "response_format ignored" here is how an honest
            # reasoning relay got blamed for our own under-budgeted request: the
            # parameter may be perfectly honoured, the answer simply never came.
            return {
                "verdict": "inconclusive",
                "parsed_as_json": False,
                "response": "",
                "finish_reason": _first_finish_reason(raw),
                "note": "回复为空，无法判断 response_format 是否生效。空回复不等于参数被忽略。",
            }
        try:
            json.loads(text)
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        return {
            "verdict": "honoured" if ok else "ignored",
            "parsed_as_json": ok,
            "response": text[:160],
        }

    def _check_logprobs(self, ctx: ProbeContext, model: str) -> dict[str, Any]:
        raw = self._raw_chat(
            ctx, model, "Say the single word: hello", max_tokens=8, logprobs=True, top_logprobs=3
        )
        choices = raw.get("choices") if isinstance(raw, dict) else None
        has = bool(
            isinstance(choices, list)
            and choices
            and isinstance(choices[0], dict)
            and choices[0].get("logprobs")
        )
        return {
            "verdict": "honoured" if has else "ignored",
            "logprobs_present": has,
            "note": "缺失 logprobs 会让依赖置信度的下游工具出错",
        }

    def _check_tools(self, ctx: ProbeContext, model: str) -> dict[str, Any]:
        raw = self._raw_chat(
            ctx,
            model,
            _TOOL_MESSAGE,
            max_tokens=160,
            tools=_TOOL_SCHEMA,
            tool_choice="auto",
        )
        choice = _first_choice(raw)
        msg = (choice or {}).get("message") or {}
        calls = msg.get("tool_calls")
        has = bool(calls)
        detail = {
            "verdict": "honoured" if has else "ignored",
            "tool_calls_present": has,
        }
        if not has:
            detail["response"] = str(msg.get("content"))[:160]
        return detail

    @staticmethod
    def _raw_chat(
        ctx: ProbeContext,
        model: str,
        prompt: str,
        *,
        grow: bool = False,
        **kw: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        body.update(kw)
        raw = _post_chat(ctx, body)
        # Same rule as ProbeContext.ask, for the checks that read the raw payload
        # instead of a Completion. A reply cut off before any usable visible text
        # exists says nothing about the parameter under test, so grow and ask
        # again rather than record an empty string — or a two-word scrap — as
        # "the parameter was ignored".
        if grow and starved_of_visible_text(_first_content(raw), _first_finish_reason(raw)):
            cap = int(body.get("max_tokens") or 0)
            bigger = grow_budget(cap)
            if bigger > cap and not ctx.out_of_budget():
                ctx.progress(
                    f"{model} 在 max_tokens={cap} 下可见正文被 reasoning 挤没了，"
                    f"以 max_tokens={bigger} 重试一次"
                )
                body["max_tokens"] = bigger
                raw = _post_chat(ctx, body)
        return raw


# --------------------------------------------------------------------- helpers


def _post_chat(ctx: ProbeContext, body: dict[str, Any]) -> dict[str, Any]:
    status, payload = ctx.client.post_json("/v1/chat/completions", body, billable=True)
    if status >= 400:
        raise RelayError(f"HTTP {status}", status=status, body=str(payload)[:200])
    return payload if isinstance(payload, dict) else {}


def _first_choice(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    choices = raw.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return None


def _first_content(raw: Any) -> str:
    choice = _first_choice(raw)
    if not choice:
        return ""
    msg = choice.get("message") or {}
    content = msg.get("content")
    return content if isinstance(content, str) else ""


def _first_finish_reason(raw: Any) -> "str | None":
    choice = _first_choice(raw)
    if not choice:
        return None
    reason = choice.get("finish_reason")
    return reason if isinstance(reason, str) else None


_MIN_COMPARABLE_CHARS = 24


def _same_text(a: str, b: str) -> "bool | None":
    """Whether two answers are meaningfully identical.

    Returns ``None`` when the pair cannot be judged at all — either side empty
    or shorter than :data:`_MIN_COMPARABLE_CHARS`. Short answers collide by
    accident ("ok" == "ok"), so a very short pair is never evidence of sameness
    *or* of difference.

    The three states matter. With a plain boolean, "both answers were empty"
    came back as ``False`` — i.e. "the two answers differ" — which turned a
    reasoning model's under-budgeted reply into a fabricated
    ``temperature 被忽略`` accusation against an honest relay.
    """
    a, b = (a or "").strip(), (b or "").strip()
    if len(a) < _MIN_COMPARABLE_CHARS or len(b) < _MIN_COMPARABLE_CHARS:
        return None
    return a == b
