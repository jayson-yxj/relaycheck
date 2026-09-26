"""Reliability probe — how well does the endpoint actually serve requests?

This probe exists because of a real deployment: a relay that answered a trivial
``max_tokens=8`` request in 2 seconds one moment and hung past a 45-second read
timeout the next, with roughly half of all requests failing.  Every other probe
in this tool assumes it can complete requests; when it cannot, the honest
outcome is "the audit is incomplete", not "the audit passed".

So this probe runs first, measures, and reports:

* the raw success rate with retries **disabled** (retries would hide exactly the
  flakiness we are trying to measure),
* latency percentiles for the requests that did succeed,
* the concrete error classes seen.

The result is stored in ``ctx.options["reliability"]`` so later probes and the
CLI can adapt to what they are talking to — a relay that cannot answer reliably
cannot be audited for substitution with any confidence, and the report has to
say so.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

from .base import Probe, ProbeContext
from ..client import RelayBudgetExceeded, RelayError
from ..models import Confidence, ProbeResult, Severity

#: a deliberately trivial request: one short word, tiny output cap
PING_PROMPT = "Reply with exactly one word: ok"

#: how many samples to take
DEFAULT_SAMPLES = 8

#: above this failure rate the endpoint is not a working service
FAILURE_RATE_HIGH = 0.20

#: above this median latency, interactive use is painful
SLOW_P50_S = 15.0


class ReliabilityProbe(Probe):
    """Measure success rate and latency before trusting any other probe."""

    name = "reliability"
    description = "中转站到底能不能稳定服务：成功率与延迟"
    needs_models = True

    def run(self, ctx: ProbeContext) -> ProbeResult:
        result = self._result()

        if not ctx.models:
            self._find(
                result,
                id="rel-000",
                title="没有可用模型，无法测量可用性",
                severity=Severity.INFO,
                confidence=Confidence.CONFIRMED,
                summary="模型列表为空，可用性测量被跳过。",
            )
            return result

        model = ctx.models[0]
        samples = int(ctx.option("reliability_samples", DEFAULT_SAMPLES) or DEFAULT_SAMPLES)
        timeout = float(
            ctx.option("reliability_timeout_s", min(ctx.client.timeout, 30.0))
        )

        # Measure raw behaviour: a retry would mask the very failures we are
        # counting, and would multiply the audit's wall-clock time by the retry
        # count on exactly the endpoints that can least afford it.
        saved_retries = ctx.client.max_retries
        saved_timeout = ctx.client.timeout
        ctx.client.max_retries = 0
        ctx.client.timeout = timeout

        observations: list[dict[str, Any]] = []
        try:
            for i in range(samples):
                if ctx.out_of_budget():
                    self._note_budget(
                        result, ctx, f"已完成 {i}/{samples} 次可用性采样。"
                    )
                    break
                t0 = time.monotonic()
                entry: dict[str, Any] = {"index": i, "model": model}
                ctx.progress(f"{model} 第 {i + 1}/{samples} 次探测中…")
                try:
                    comp = ctx.client.chat(
                        model,
                        [{"role": "user", "content": PING_PROMPT}],
                        max_tokens=8,
                        temperature=0,
                    )
                    entry["ok"] = True
                    entry["http_status"] = comp.http_status
                    entry["content"] = comp.content[:80]
                except RelayBudgetExceeded:
                    # Running out of budget is NOT a failure of the relay. Counting
                    # it as one would report a merely-slow endpoint as an unreliable
                    # one — a false accusation produced by our own timeout.
                    self._note_budget(
                        result, ctx, f"已完成 {i}/{samples} 次可用性采样。"
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - failure is the measurement
                    entry["ok"] = False
                    # ``type(exc).__name__`` alone renders every failure as the
                    # useless string "RelayError". A report that says six requests
                    # failed without saying *why* cannot be acted on, and cannot be
                    # checked by the reader.
                    entry["error_type"] = type(exc).__name__
                    entry["error"] = (
                        exc.describe(200)
                        if isinstance(exc, RelayError)
                        else f"{type(exc).__name__}: {str(exc)[:200]}"
                    )
                    # ``None`` status means nothing ever came back (timeout / reset);
                    # an integer means the relay answered, just not usefully. Those
                    # are different problems and the report must not merge them.
                    entry["http_status"] = (
                        exc.status if isinstance(exc, RelayError) else None
                    )
                entry["latency_s"] = round(time.monotonic() - t0, 3)
                observations.append(entry)
        finally:
            ctx.client.max_retries = saved_retries
            ctx.client.timeout = saved_timeout

        ok = [o for o in observations if o["ok"]]
        failed = [o for o in observations if not o["ok"]]
        attempted = len(observations)
        failure_rate = (len(failed) / attempted) if attempted else 0.0
        latencies = [o["latency_s"] for o in ok]

        # 「失败」不是一个东西，是三个。只有第一个是可用性问题：
        #   * 没回来（超时/连接断），或者回来了 5xx —— 端点是**真的**没服务成这个
        #     极简请求；
        #   * 429 —— 端点其实**正常工作**，它在叫我们慢一点。官方直连 API 在
        #     --delay 0.4 的连发下就会这样回。把这种算成「极不稳定」，等于拿我们
        #     自己的节奏去指控一个健康的端点；
        #   * 其他 4xx —— 请求本身被拒（形状不对、key 不对、模型不存在）。同样不
        #     是「端点撑不住」的证据。
        # 所以能升级到 HIGH 的只有第一个桶；另外两个照实记下来，进 INFO。
        unavailable = [
            o for o in failed
            if o.get("http_status") is None or o["http_status"] >= 500
        ]
        throttled = [o for o in failed if o.get("http_status") == 429]
        refused = [
            o
            for o in failed
            if o.get("http_status") is not None
            and 400 <= o["http_status"] < 500
            and o["http_status"] != 429
        ]
        unavailability = (len(unavailable) / attempted) if attempted else 0.0

        stats: dict[str, Any] = {
            "model": model,
            "samples": attempted,
            "succeeded": len(ok),
            "failed": len(failed),
            "failure_rate": round(failure_rate, 3),
            #: 只有这一项能升级成 HIGH；failure_rate 只是「没成功」的原始比例，
            #: 它把限流和我们自己发歪的请求也算进去了，不能直接当结论用。
            "unavailability_rate": round(unavailability, 3),
            "unavailable": len(unavailable),
            "throttled": len(throttled),
            "refused": len(refused),
            "timeout_s": timeout,
            "retries_disabled": True,
        }
        if latencies:
            stats["p50_latency_s"] = round(statistics.median(latencies), 3)
            stats["min_latency_s"] = round(min(latencies), 3)
            stats["max_latency_s"] = round(max(latencies), 3)
            if len(latencies) > 1:
                stats["mean_latency_s"] = round(statistics.fmean(latencies), 3)
        if failed:
            stats["errors"] = sorted({o["error"] for o in failed})
            stats["failures"] = [
                {
                    "index": o["index"],
                    "latency_s": o["latency_s"],
                    "http_status": o.get("http_status"),
                    "error": o["error"],
                }
                for o in failed
            ]
            # Two different diseases, two different remedies: a request that never
            # came back is a capacity/timeout problem, a request that came back with
            # 4xx/5xx is a configuration or upstream problem. Saying "N requests
            # timed out" when N requests actually returned HTTP 400 is simply false,
            # and the reader would go argue about the wrong thing.
            stats["timeout_failures"] = sum(
                1 for o in failed if o.get("http_status") is None
            )
            stats["error_response_failures"] = sum(
                1 for o in failed if o.get("http_status") is not None
            )

        result.data["reliability"] = stats
        result.data["observations"] = observations

        # Publish for other probes / the CLI. Deliberately not used to silently
        # shorten other probes: that would turn a flaky relay into a clean report.
        ctx.options["reliability"] = stats

        if attempted == 0:
            self._find(
                result,
                id="rel-001",
                title="可用性采样一次都没跑完",
                severity=Severity.INFO,
                confidence=Confidence.CONFIRMED,
                summary=(
                    "时间预算在第一次可用性采样前就用尽了，"
                    "本次审计对中转站的可用性没有任何结论。"
                ),
                evidence=stats,
            )
            return result

        if unavailability > FAILURE_RATE_HIGH:
            timeouts = sum(1 for o in unavailable if o.get("http_status") is None)
            err_resp = sum(1 for o in unavailable if o.get("http_status") is not None)
            if err_resp and not timeouts:
                how = (
                    f"{len(unavailable)} 次都返回了错误响应"
                    f"（最典型的是 {stats['errors'][0][:80]}），没有一次正常完成"
                )
            elif timeouts and not err_resp:
                how = f"{len(unavailable)} 次在 {timeout:.0f} 秒内都没有返回"
            else:
                how = (
                    f"其中 {timeouts} 次在 {timeout:.0f} 秒内没有返回，"
                    f"{err_resp} 次直接返回了 5xx"
                )
            self._find(
                result,
                id="rel-100",
                title=f"中转站极不稳定：{len(unavailable)}/{attempted} 次极简请求没有服务成",
                severity=Severity.HIGH,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"用最简请求（要求只回一个词、上限 8 token）连发 {attempted} 次，"
                    f"{how}，不可用率 {unavailability:.0%}。"
                    "而且这是**关闭重试**测出来的原始成功率。"
                    "一个连这种请求都服务不好的中转站，既不能稳定给你干活，"
                    "也让下面所有探针的结论都可能不完整——先解决可用性，再谈掉包。"
                ),
                evidence=stats,
                remediation=(
                    "先看 evidence.failures 里的 error 字段："
                    "若是超时，问服务方上游是否限流/超卖；"
                    "若是 5xx，那通常是它自己的渠道配置坏了，要求其给出上游原始报错。"
                    "在稳定性恢复前，不要用它跑批量任务；"
                    "本次审计报告中标注为「不完整」的项目，都需要在稳定期重跑。"
                ),
            )
        elif (
            stats.get("p50_latency_s", 0) > SLOW_P50_S
            and not (throttled or refused)
        ):
            # Only judge speed from requests the endpoint actually served. When
            # part of the run came back 429/4xx, the queue we were put in is
            # folded into the median — that number describes the endpoint's
            # rate limiting, not its latency. Official first-party APIs throttle
            # under load as a matter of policy, so the naive ordering accused
            # exactly the honest endpoints this check is supposed to clear.
            self._find(
                result,
                id="rel-101",
                title=f"中转站响应很慢：中位延迟 {stats['p50_latency_s']:.1f} 秒",
                severity=Severity.MEDIUM,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"最简请求的中位延迟 {stats['p50_latency_s']:.1f} 秒"
                    f"（最快 {stats.get('min_latency_s')} 秒 / 最慢 {stats.get('max_latency_s')} 秒），"
                    f"成功率 {len(ok)}/{attempted}。延迟这么高，通常意味着上游是共享池、"
                    "排队严重或者被限速，而不是直连官方 API。"
                ),
                evidence=stats,
            )
        elif throttled or refused:
            # 「一次都没成功」和「端点撑不住」不是一回事。走到这里全是 429/4xx：
            # 请求回来了，端点按规矩回绝了我们。它不足以证明端点健康（所以不能发
            # rel-clean —— 那就是把「没测到」写成「没问题」），但更没资格指控端点
            # 不稳定：那正是本工具最不该犯的错。
            parts = []
            if throttled:
                parts.append(f"{len(throttled)} 次被限流（429）")
            if refused:
                parts.append(f"{len(refused)} 次被拒绝（4xx）")
            p50 = stats.get("p50_latency_s")
            slow_note = ""
            if p50 is not None and p50 > SLOW_P50_S:
                slow_note = (
                    f"另外中位延迟 {p50:.1f} 秒偏高，但那是在被回绝的条件下量到的，"
                    "不能拿来判断端点本身快不快。"
                )
            self._find(
                result,
                id="rel-102",
                title=f"可用性没能测出来：{attempted - len(ok)}/{attempted} 次被端点主动回绝",
                severity=Severity.INFO,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"用最简请求（要求只回一个词、上限 8 token）连发 {attempted} 次，"
                    f"成功 {len(ok)} 次，" + "、".join(parts) + "。"
                    "这些都不算「端点撑不住」：429 是端点正常工作、叫我们慢一点，"
                    "4xx 是这一次请求本身被拒（形状不对、key 不对、模型不存在）。"
                    "所以可用性这一项本次**没有结论**，"
                    "换更长的 --delay 或更少的 --reliability-samples 再跑一次。"
                    + slow_note
                ),
                evidence=stats,
            )
        else:
            self._find(
                result,
                id="rel-clean",
                title="可用性正常",
                severity=Severity.INFO,
                confidence=Confidence.CONFIRMED,
                summary=(
                    f"最简请求 {len(ok)}/{attempted} 成功，"
                    f"中位延迟 {stats.get('p50_latency_s', float('nan')):.2f} 秒。"
                ),
                evidence=stats,
            )

        return result
