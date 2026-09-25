"""响应体自己说它答的是哪个模型 —— 整个工具里最便宜的一条硬证据。

每个兼容 OpenAI 协议的网关都**必须**在回复里填 ``model`` 字段。一个把请求真的
转发到另一个后端的站，通常会把后端的名字原样带出来：改写它属于额外的工作，而这个
字段又不是客户端会检查的东西。于是「你付钱买的模型名」和「上游自称的模型名」可以
被摆在同一条证据里，代价只是每个模型两次请求。

这条探针不把那个字段当口供：

* 字段可能是站方自己填的，可能被统一改写成售卖名，也可能为空 —— 所以「没填」
  和「填了别的」是两个不同结论，且都不是「没问题」；
* 名字不同**不等于**掉包 —— ``gpt-4o`` 被上游规范化成 ``gpt-4o-2024-11-20`` 是
  常见的正常行为（版本固定），这种情况只报 INFO；
* 只有**跨厂商**的名字冲突（请求 deepseek，响应自称 claude）才构成指控，而且仍然是
  LIKELY 而不是 CONFIRMED：站方可能只是套了一层自己的命名，也可能真的换了后端 ——
  这一条分不出来，所以它的措辞不能替你下结论。

完整返回与流式返回各查一次。两者是站方要分别填写的两个位置，未必填成同一个名字；
只查一个会漏掉「一个路径改名、另一个路径漏了」这种最常见的不一致。
"""

from __future__ import annotations

from typing import Any

from ..client import RelayBudgetExceeded, RelayError
from ..families import claimed_family
from ..models import Confidence, ProbeResult, Severity
from .base import Probe, ProbeContext

#: 一次只要几个 token。这条探针看的是响应里的元数据，不是正文。
_MAX_TOKENS = 16

#: 便宜到可以只发一个字节。内容不参与判定，所以刻意让它没有信息量。
_PROMPT = "hi"

#: 每模型两次请求。默认覆盖 6 个模型 = 12 次请求，是全部探针里最便宜的一条。
DEFAULT_MAX_MODELS = 6

def _classify(requested: str, returned: str | None) -> tuple[str, str | None]:
    """把一次响应里的 ``model`` 字段与请求的模型名对照。

    Returns ``(verdict, family_of_returned)`` where ``verdict`` is one of
    ``match`` / ``same_vendor`` / ``cross_vendor`` / ``unknown_name`` /
    ``unreported``.
    """
    name = (returned or "").strip()
    if not name:
        # Some gateways omit the field entirely. That is not "the name matched";
        # it is "this was not checked", and the caller must say so.
        return "unreported", None
    if name == requested.strip():
        return "match", None

    have = claimed_family(name)
    want = claimed_family(requested)
    if have and want and have == want:
        # Same vendor, different string: version pinning, a date suffix, a
        # ``vendor/model`` path form. Normal behaviour, reported at INFO.
        return "same_vendor", have
    if have and want:
        return "cross_vendor", have
    # At least one of the two names does not advertise a known vendor, so the
    # vendors cannot be compared. Say that instead of guessing.
    return "unknown_name", have


class EchoProbe(Probe):
    name = "echo"
    description = "对照「你请求的模型名」与「响应体自称的模型名」，完整与流式各一次"

    #: 默认覆盖多少个模型（每模型两次请求）。
    max_models_default = DEFAULT_MAX_MODELS

    def _one_path(
        self, ctx: ProbeContext, model: str, path: str
    ) -> dict[str, Any]:
        messages = [{"role": "user", "content": _PROMPT}]
        try:
            if path == "plain":
                comp = ctx.ask(model, messages, max_tokens=_MAX_TOKENS, temperature=0, grow=False)
                returned = comp.model_returned
                http_status = comp.http_status
            else:
                streamed = ctx.client.chat_stream(
                    model, messages, max_tokens=_MAX_TOKENS, temperature=0
                )
                returned = streamed.model_returned
                http_status = streamed.http_status
        except RelayError as exc:
            return {
                "path": path,
                "model_returned": None,
                "claimed_family": None,
                "verdict": "error",
                "http_status": exc.status,
                "error": exc.describe(120),
            }

        verdict, family = _classify(model, returned)
        return {
            "path": path,
            "model_returned": returned,
            "claimed_family": family,
            "verdict": verdict,
            "http_status": http_status,
            "error": None,
        }

    def run(self, ctx: ProbeContext) -> ProbeResult:
        result = self._result()
        max_models = int(ctx.option("echo_max_models", self.max_models_default))
        models = list(ctx.models)[:max_models]

        observations: dict[str, Any] = {}
        # Every bucket counts *path observations*, not models: the plain and the
        # streaming response are two independent places where a relay has to fill
        # in the same field, and a relay that gets one right and the other wrong
        # has still told us something. Merging the two per model would either bury
        # a leak or invent one.
        matched: list[dict[str, Any]] = []
        same_vendor: list[dict[str, Any]] = []
        cross_vendor: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        errored: list[dict[str, Any]] = []
        attempted = 0
        truncated = False

        for model in models:
            if ctx.out_of_budget():
                self._note_budget(
                    result, ctx, f"已完成 {len(observations)}/{len(models)} 个模型的自报名采集。"
                )
                truncated = True
                break

            expected = claimed_family(model)
            paths: dict[str, Any] = {}
            for path in ("plain", "stream"):
                if ctx.out_of_budget():
                    self._note_budget(
                        result,
                        ctx,
                        f"采集 {model} 的响应自称时超出预算"
                        f"（已完成 {len(observations)}/{len(models)} 个模型）。",
                    )
                    truncated = True
                    break
                try:
                    observed = self._one_path(ctx, model, path)
                except RelayBudgetExceeded:
                    self._note_budget(
                        result,
                        ctx,
                        f"采集 {model} 的响应自称时超出预算"
                        f"（已完成 {len(observations)}/{len(models)} 个模型）。",
                    )
                    truncated = True
                    break
                observed["model_requested"] = model
                observed["expected_family"] = expected
                paths[path] = observed
                attempted += 1
                if observed["verdict"] == "error":
                    errored.append(observed)
                elif observed["verdict"] == "match":
                    matched.append(observed)
                elif observed["verdict"] == "same_vendor":
                    same_vendor.append(observed)
                elif observed["verdict"] == "cross_vendor":
                    cross_vendor.append(observed)
                else:
                    unresolved.append(observed)

            if paths:
                observations[model] = {
                    "model_requested": model,
                    "expected_family": expected,
                    "paths": paths,
                }
            if truncated:
                break

        result.data["observations"] = observations
        # A model counts as *checked* only if at least one of its two paths came
        # back with something to compare. ``observations`` is keyed by model and
        # keeps failed paths too, so it must never be used as "how many models we
        # actually got an answer for" — on a dead station that would read "3 of 3
        # models produced a comparable response, and 6 requests failed".
        checked = sorted(
            {
                o["model_requested"]
                for o in (*matched, *same_vendor, *cross_vendor, *unresolved)
            }
        )
        result.data["models_checked"] = len(checked)
        result.data["models_total"] = len(models)
        result.data["models_matched"] = sorted({o["model_requested"] for o in matched})

        # "Everything matched" may only be claimed when every model was actually
        # checked on both paths. A failed call or an exhausted budget leaves that
        # observation unverified — and unverified is not clean.
        complete = bool(models) and not errored and not truncated and attempted == len(models) * 2

        if cross_vendor:
            worst = cross_vendor[0]
            backends = sorted({o["model_returned"] for o in cross_vendor if o["model_returned"]})
            families = sorted({o["claimed_family"] for o in cross_vendor if o["claimed_family"]})
            self._find(
                result,
                id="echo-100",
                title="响应体自称的模型属于另一个厂商",
                severity=Severity.MEDIUM,
                confidence=Confidence.LIKELY,
                summary=(
                    f"共 {len(cross_vendor)} 处响应在 ``model`` 字段里自报的名字，与请求的"
                    "模型名不属于同一个厂商。"
                    f"例如以「{worst['model_requested']}」发出的请求（厂商 "
                    f"{worst['expected_family']}），响应体自称是 "
                    f"{worst['model_returned']}（厂商 {worst['claimed_family']}）。"
                    f"全部陌生的自报名：{', '.join(backends)}，涉及厂商：{', '.join(families)}。"
                    "这个字段由上游填写，站方不改写它，通常意味着请求真的落在了另一个"
                    "后端上。"
                    "**但它不是铁证**：站方也可能套了一层自己的第三方上游命名，或只是"
                    "把响应原样透传而路由与计费都没有问题。请把这一条与 twins、"
                    "tokenizer 两条放在一起看，以那两条的结论为准。"
                ),
                evidence={
                    "cross_vendor": cross_vendor,
                    "self_reported_backends": backends,
                    "models_checked": len(checked),
                    "models_total": len(models),
                },
                remediation=(
                    "核对下单的模型与实际收到的模型是否同一个；若站方坚持是同一个，"
                    "要求它给出这次请求的上游调用记录。同时运行 "
                    "--probes tokenizer,twins 做行为层面的交叉验证。"
                ),
            )

        if same_vendor:
            worst = same_vendor[0]
            self._find(
                result,
                id="echo-101",
                title="响应体返回的模型名与请求不同（同厂商）",
                severity=Severity.INFO,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"共 {len(same_vendor)} 处响应的 ``model`` 字段与请求名不同，但属于"
                    "同一厂商。"
                    f"例如请求「{worst['model_requested']}」，响应体自称 "
                    f"{worst['model_returned']}。"
                    "**这不是指控**：上游把名字规范化成带日期或版本的正式名"
                    "（``gpt-4o`` → ``gpt-4o-2024-11-20``）、或在名字前加厂商路径，"
                    "都是正常行为，站方往往只是原样透传。"
                    "本条只把事实记下来：你实际拿到的那个名字是哪一个。"
                ),
                evidence={
                    "same_vendor": same_vendor,
                    "models_checked": len(checked),
                    "models_total": len(models),
                },
            )

        if unresolved:
            self._find(
                result,
                id="echo-102",
                title="响应体自报的模型名无法与请求名对照",
                severity=Severity.INFO,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"共 {len(unresolved)} 处响应无法判定：要么响应体根本没有返回 "
                    "``model`` 字段，要么两个名字里至少有一个看不出属于哪家厂商，"
                    "因此无法比较。"
                    "**这一项没有被检验，它的结论是「不知道」，不是「没问题」。**"
                ),
                evidence={
                    "unresolved": unresolved,
                    "models_checked": len(checked),
                    "models_total": len(models),
                },
            )

        if not (cross_vendor or same_vendor or unresolved):
            if complete:
                self._find(
                    result,
                    id="echo-clean",
                    title="响应体自报的模型名与请求一致",
                    severity=Severity.CLEAN,
                    confidence=Confidence.CONFIRMED,
                    summary=(
                        f"{len(models)} 个模型的完整返回与流式返回都自报了自己被请求的"
                        "名字。上游没有暴露任何陌生的后端名。"
                    ),
                    evidence={
                        "matched": matched,
                        "models_checked": len(checked),
                        "models_total": len(models),
                    },
                )
            else:
                # Either nothing came back, or some model was never reached.
                # Both are "not checked" — and not checked may never be CLEAN.
                self._find(
                    result,
                    id="echo-000",
                    title="未能完成模型自报名的对照",
                    severity=Severity.INFO,
                    confidence=Confidence.CONFIRMED,
                    summary=(
                        f"{len(models)} 个模型里只有 {len(checked)} 个拿到了可用于"
                        f"对照的响应（{attempted - len(errored)}/{attempted} 处请求成功）"
                        + (f"，另有 {len(errored)} 处请求直接失败" if errored else "")
                        + ("；本次运行因超时预算提前结束" if truncated else "")
                        + "。这一项没有被完整检验，它的结论是「不知道」，不是「没问题」。"
                    ),
                    evidence={
                        "observations": observations,
                        "errored": errored,
                        "models_checked": len(checked),
                        "models_total": len(models),
                    },
                )

        return result
