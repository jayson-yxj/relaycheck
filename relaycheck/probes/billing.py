"""Billing probes.

Three independent things can be wrong with how a relay charges you:

1. **Hidden reasoning billed as output.** A reasoning model that hides its
   chain-of-thought but still bills it at output price. You pay 10-50x more
   tokens than the answer you can actually see.
2. **A lying billing basis.** The merchant says "we bill per request" while the
   panel is configured for token billing, or claims a multiplier it does not
   apply.
3. **Margin you can measure.** Some panels expose their own upstream cost
   (``account_cost``). When they do, the markup stops being a guess.

All three are detectable from the client side, and none of them require
guessing: they are arithmetic on numbers the relay itself returns.
"""

from __future__ import annotations

from typing import Any

from ..client import RelayError
from ..models import Confidence, ProbeResult, Severity
from .base import Probe, ProbeContext

#: Prompt designed to produce a *short* visible answer, so any large billed
#: token count cannot be explained by the answer itself.
SHORT_ANSWER_PROMPT = "Reply with exactly one word: ok"

#: Paths that identify a sub2api-family panel and expose billing configuration.
PANEL_PATHS: dict[str, str] = {
    "/v1/sub2api/billing": "sub2api 计费口径（authoritative billing scope）",
    "/v1/usage": "用量与余额（可能含上游成本 account_cost）",
    "/api/v1/settings/public": "公开设置（功能开关）",
    "/api/v1/model-plaza": "模型广场（分组倍率 / 计费模式）",
    "/api/v1/setup/status": "安装状态",
    "/health": "健康检查",
}

#: Rough tokens-per-character for a *visible* answer. Used only as a sanity
#: band — the real signal is the ratio, not the absolute estimate.
_ASCII_TOKENS_PER_CHAR = 0.28
_CJK_TOKENS_PER_CHAR = 0.70

#: A charged/upstream ratio at or below this is treated as pass-through pricing
#: rather than a markup. The band absorbs rounding and currency drift; the point
#: is to keep「面板暴露了加价」from firing on a panel whose own ledger says there
#: is no markup.
_MARKUP_TOLERANCE = 1.05


class HiddenReasoningProbe(Probe):
    name = "billing-hidden-reasoning"
    description = "检测思维链被当成输出 token 计费、却对用户隐藏"

    def run(self, ctx: ProbeContext) -> ProbeResult:
        result = self._result()
        models = list(ctx.models)
        if not models:
            result.error = "no models supplied"
            return result

        observations: dict[str, Any] = {}
        suspicious: list[dict[str, Any]] = []

        for model in models:
            try:
                comp = ctx.client.chat(
                    model,
                    [{"role": "user", "content": SHORT_ANSWER_PROMPT}],
                    max_tokens=512,
                    temperature=0,
                )
            except RelayError as exc:
                observations[model] = {"error": exc.describe()}
                continue

            billed = comp.usage.completion_tokens
            visible_chars = comp.visible_chars
            reported_reasoning = comp.usage.reasoning_tokens
            has_visible_reasoning_field = _has_reasoning_content(comp.raw)

            record: dict[str, Any] = {
                "visible_chars": visible_chars,
                "visible_text": comp.content[:120],
                "completion_tokens": billed,
                "reasoning_tokens_reported": reported_reasoning,
                "visible_reasoning_field": has_visible_reasoning_field,
            }

            if billed is None:
                record["verdict"] = "no-usage"
                observations[model] = record
                continue

            expected = _estimate_visible_tokens(comp.content)
            ratio = billed / visible_chars if visible_chars else float("inf")
            record["estimated_visible_tokens"] = round(expected, 1)
            record["billed_vs_estimated_ratio"] = (
                round(billed / expected, 2) if expected > 0 else None
            )
            record["billed_tokens_per_visible_char"] = (
                round(ratio, 2) if visible_chars else None
            )

            # A model that shows its reasoning is not cheating; it is just verbose.
            if has_visible_reasoning_field:
                record["verdict"] = "visible-reasoning-ok"
            elif reported_reasoning is not None and reported_reasoning > 0:
                record["verdict"] = "reported-reasoning-ok"
            elif expected > 0 and billed > max(expected * 1.8, expected + 24):
                record["verdict"] = "hidden-reasoning-suspected"
                suspicious.append({"model": model, **record})
            else:
                record["verdict"] = "ok"

            observations[model] = record

        result.data["observations"] = observations

        if suspicious:
            worst = max(
                suspicious,
                key=lambda r: r.get("billed_vs_estimated_ratio") or 0,
            )
            self._find(
                result,
                id="bill-100",
                title="疑似把隐藏的思维链按输出价格计费",
                severity=Severity.HIGH,
                confidence=Confidence.LIKELY,
                summary=(
                    f"共 {len(suspicious)} 个模型在极短回答下仍产生了远超可见内容的 "
                    "completion_tokens，且响应中既没有 reasoning_content 字段，"
                    "也没有上报 reasoning_tokens。"
                    "这意味着思维链被隐藏了、但照常收你输出费。"
                    f"最严重的是 {worst['model']}：可见 {worst['visible_chars']} 字符，"
                    f"却计费 {worst['completion_tokens']} output tokens。"
                ),
                evidence={"suspicious_models": suspicious},
                remediation=(
                    "要求中转站出示上游 usage 原始返回；"
                    "若思维链确实被隐藏，应按隐藏部分的价格（通常更低）或不计费处理。"
                ),
            )
        else:
            # Only a model that returned a usable usage block can be cleared.
            # A failed request has no ratio to judge, so treating it as "no
            # hidden reasoning" would clear a relay that never answered.
            measured = [
                o
                for o in observations.values()
                if o.get("billed_vs_estimated_ratio") is not None
            ]
            if not measured:
                self._find(
                    result,
                    id="bill-000",
                    title="隐藏 reasoning 计费未能检测",
                    severity=Severity.INFO,
                    confidence=Confidence.CONFIRMED,
                    summary=(
                        f"{len(models)} 个模型都没有返回可用的 usage，"
                        "无法比较 completion_tokens 与可见内容。这一项没有被检验。"
                    ),
                    evidence={"observations": observations},
                )
            else:
                self._find(
                    result,
                    id="bill-clean",
                    title="未发现隐藏 reasoning 计费",
                    severity=Severity.CLEAN,
                    confidence=Confidence.CONFIRMED,
                    summary=(
                        f"{len(measured)}/{len(models)} 个模型完成了比对："
                        "短回答下 completion_tokens 与可见内容量级一致，"
                        "未发现思维链被隐藏计费的迹象。"
                    ),
                    evidence={
                        "models_measured": len(measured),
                        "observations": observations,
                    },
                )

        return result


class PanelBillingProbe(Probe):
    name = "billing-panel"
    description = "读面板接口自报的计费口径、倍率与上游成本"

    def run(self, ctx: ProbeContext) -> ProbeResult:
        result = self._result()
        found: dict[str, Any] = {}

        for path, label in PANEL_PATHS.items():
            try:
                status, payload = ctx.client.get_json(path)
            except RelayError as exc:
                found[path] = {"label": label, "error": str(exc)[:160]}
                continue
            entry: dict[str, Any] = {"label": label, "status": status}
            if isinstance(payload, dict):
                # Keep the response small but complete enough to be evidence.
                entry["body"] = _trim(payload, depth=3)
            else:
                entry["body"] = str(payload)[:400]
            found[path] = entry

        result.data["endpoints"] = found

        billing = found.get("/v1/sub2api/billing", {})
        body = billing.get("body") if isinstance(billing, dict) else None
        if isinstance(body, dict) and billing.get("status") == 200:
            scope = _dig(body, "billing_scope")
            multiplier = _dig(body, "effective_rate_multiplier") or _dig(
                body, "resolved_rate_multiplier"
            )
            self._find(
                result,
                id="bill-200",
                title="识别为 sub2api 面板，并读到其自称的计费口径",
                severity=Severity.INFO,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"该中转站运行 sub2api 面板，接口自报 billing_scope="
                    f"{scope!r}、effective_rate_multiplier={multiplier!r}。"
                    "这是面板配置的权威值——如果商家的口头说法与它矛盾，以这里为准。"
                ),
                evidence={"billing_scope": scope, "rate_multiplier": multiplier, "raw": body},
                remediation=(
                    "将该值与商家的计费说明逐条对照；"
                    "billing_scope=token 时，任何『按请求数计费』的说法都是假的。"
                ),
            )

        usage_body = _dig(found.get("/v1/usage", {}), "body")
        margins = _extract_margins(usage_body)
        reachable = [p for p, d in found.items() if "error" not in d]
        result.data["endpoints_reachable"] = sorted(reachable)
        if margins:
            result.data["per_model_margin"] = margins
            worst = max(margins, key=lambda m: m["markup_x"])
            if worst["markup_x"] > _MARKUP_TOLERANCE:
                self._find(
                    result,
                    id="bill-201",
                    title="面板自报的上游成本暴露了各模型加价倍率",
                    severity=Severity.MEDIUM,
                    confidence=Confidence.CONFIRMED,
                    summary=(
                        "该面板在用量接口里同时返回了『向你收取的金额』(cost) 和"
                        "『它自己记的上游成本』(account_cost)。两者相除即为真实加价倍率。"
                        f"其中 {worst['model']} 加价 {worst['markup_x']}x。"
                        "这是平台自己的账，无法用话术否认。"
                    ),
                    evidence={"per_model_margin": margins},
                    remediation="用于对账与议价；要求商家解释加价倍率与其宣传是否一致。",
                )
            else:
                # The panel does disclose its own upstream cost — but the numbers
                # say it charges the same amount. Keeping the MEDIUM severity and
                # the「暴露了加价倍率」title here would tell the reader that a
                # markup was found when the panel's own ledger says the opposite:
                # a false alarm about an honest relay, built out of our own
                # success at reading its books. The disclosure is still a fact
                # worth recording, so it stays as INFO.
                self._find(
                    result,
                    id="bill-201",
                    title=f"面板公开了上游成本，未发现加价（最高 {worst['markup_x']}x）",
                    severity=Severity.INFO,
                    confidence=Confidence.CONFIRMED,
                    summary=(
                        "该面板在用量接口里同时返回了『向你收取的金额』(cost) 和"
                        "『它自己记的上游成本』(account_cost)，两者相除即为真实加价倍率。"
                        f"实测 {len(margins)} 个模型的最高倍率为 {worst['markup_x']}x"
                        f"（{worst['model']}），低于实质性加价阈值 {_MARKUP_TOLERANCE}x，"
                        "即按上游成本原价转售。"
                    ),
                    evidence={"per_model_margin": margins},
                    remediation="无需处理；该数据可用于对账与续费前的比价。",
                )
        elif reachable:
            self._find(
                result,
                id="bill-clean",
                title="面板可达，但未暴露上游成本",
                severity=Severity.CLEAN,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"探测了 {len(found)} 个面板端点，{len(reachable)} 个可达，"
                    "但都没有同时给出 cost 与 account_cost，"
                    "因此无法从平台自己的账上测算加价倍率。"
                ),
                evidence={
                    "endpoints_reachable": sorted(reachable),
                    "endpoints_probed": list(PANEL_PATHS),
                },
            )
        else:
            # "No panel answered" is not the same as "the panel hid nothing".
            self._find(
                result,
                id="bill-202",
                title="面板端点全部不可达",
                severity=Severity.INFO,
                confidence=Confidence.CONFIRMED,
                summary=(
                    "所有面板端点都没有正常响应，既读不到计费口径，也读不到上游成本。"
                    "这一项没有被检验。"
                ),
                evidence={"endpoints_probed": found},
            )

        return result


# --------------------------------------------------------------------- helpers


def _has_reasoning_content(payload: dict[str, Any]) -> bool:
    for choice in payload.get("choices") or []:
        msg = choice.get("message") or {}
        for key in ("reasoning_content", "reasoning"):
            v = msg.get(key)
            if isinstance(v, str) and v.strip():
                return True
    return False


def _estimate_visible_tokens(text: str) -> float:
    """Very rough token estimate for the *visible* answer."""
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk * _CJK_TOKENS_PER_CHAR + other * _ASCII_TOKENS_PER_CHAR


def _trim(obj: Any, depth: int) -> Any:
    """Shrink a nested payload for evidence storage.

    Scalars are always preserved. Only *containers* are elided once the depth
    budget runs out — truncating a scalar would silently destroy the numbers a
    finding is built on.
    """
    if isinstance(obj, dict):
        if depth <= 0:
            return "…"
        return {k: _trim(v, depth - 1) for k, v in list(obj.items())[:24]}
    if isinstance(obj, list):
        if depth <= 0:
            return "…"
        return [_trim(v, depth - 1) for v in obj[:8]]
    if isinstance(obj, str) and len(obj) > 300:
        return obj[:300] + "…"
    return obj


def _dig(obj: Any, *keys: str) -> Any:
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _extract_margins(usage_body: Any) -> list[dict[str, Any]]:
    """Pull charged-vs-upstream cost pairs out of a usage payload."""
    if not isinstance(usage_body, dict):
        return []
    stats = usage_body.get("model_stats")
    if not isinstance(stats, list):
        return []

    out: list[dict[str, Any]] = []
    for row in stats:
        if not isinstance(row, dict):
            continue
        charged = row.get("cost")
        upstream = row.get("account_cost")
        if not isinstance(charged, (int, float)) or not isinstance(upstream, (int, float)):
            continue
        if upstream <= 0:
            continue
        out.append({
            "model": row.get("model"),
            "requests": row.get("requests"),
            "charged": round(float(charged), 6),
            "upstream_cost": round(float(upstream), 6),
            "markup_x": round(float(charged) / float(upstream), 2),
        })
    return sorted(out, key=lambda m: m["markup_x"], reverse=True)
