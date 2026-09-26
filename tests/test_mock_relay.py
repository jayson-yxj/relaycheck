"""End-to-end acceptance test for relaycheck.

Runs the full probe set against the local mock relay in each of its eight
scenarios, plus focused runs for the checks that need one:

``fraudulent``
    The detector must produce concrete, correctly-classified findings.

``clean``
    The detector must produce **no** non-CLEAN finding other than INFO. This is
    the half that matters most: a tool that cries wolf on an honest relay is
    worse than no tool at all, because it launders a real accusation into noise.

``same-vendor`` / ``slow`` / ``dead``
    The three false-positive traps: a legitimate alias pair, a relay that is slow
    rather than dishonest, and a relay that answers nothing.

``reasoning`` / ``noisy`` / ``unstable-self``
    The three ways a station can be honest and still look wrong: a backend that
    returns reasoning and no visible text, a backend that cannot repeat itself,
    and a backend that will not give the same answer twice about its own origin.
    None of the three is evidence of anything, and each one broke the tool once.

Run with pytest, or directly::

    python tests/test_mock_relay.py
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_relay  # noqa: E402  (path set up above)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relaycheck.client import RelayClient  # noqa: E402
from relaycheck.models import Confidence, Severity  # noqa: E402
from relaycheck.probes import ALL_PROBES, select_probes  # noqa: E402
from relaycheck.probes.base import ProbeContext, run_probes  # noqa: E402
from relaycheck.cli import _make_output_safe  # noqa: E402
from relaycheck.reporter import Report  # noqa: E402

# The checks below print finding titles, and those titles are Chinese. A Windows
# console usually runs a legacy code page (the GitHub ``windows-latest`` runner
# gives cp1252, which cannot represent any CJK glyph), so an unguarded print()
# raises UnicodeEncodeError *inside the check* and gets reported as a failing
# test even though every assertion held. cli.main() already guards its own
# output; when the suite is driven directly, as CI does with
# ``python tests/test_mock_relay.py``, nothing else has, so the harness opts
# into the same protection.
_make_output_safe()

FRAUDULENT_MODELS = ["gpt-4o", "gemini-1.5-pro", "claude-3-5-sonnet", "deepseek-chat"]
CLEAN_MODELS = ["gpt-4o", "claude-3-5-sonnet", "deepseek-chat"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Server:
    def __init__(self, scenario: str, **serve_kwargs: Any) -> None:
        self.port = _free_port()
        self.httpd = mock_relay.serve(self.port, scenario, **serve_kwargs)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._wait_ready()

    def _wait_ready(self, timeout: float = 5.0) -> None:
        import urllib.request

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.base_url}/health", timeout=0.5):
                    return
            except Exception:  # noqa: BLE001
                time.sleep(0.05)
        raise RuntimeError("mock relay did not start")

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def run_audit(
    scenario: str,
    models: list[str],
    *,
    options: dict[str, Any] | None = None,
    probe_names: list[str] | None = None,
    **serve_kwargs: Any,
) -> Report:
    server = _Server(scenario, **serve_kwargs)
    try:
        client = RelayClient(server.base_url, mock_relay.TEST_API_KEY, delay_between_requests=0.0)
        ctx = ProbeContext(
            client=client, models=models, max_calls=400, options=dict(options or {})
        )
        probes = select_probes(probe_names) if probe_names else list(ALL_PROBES)
        results = run_probes(probes, ctx)
        return Report(
            target=server.base_url,
            models=models,
            available_models=models,
            probe_names=[p.name for p in probes],
            results=results,
            tool_version="test",
            started_at="test",
            duration_s=0.0,
            requests_made=client.request_count,
        )
    finally:
        server.close()


def _ids(report: Report, severities: set[Severity]) -> set[str]:
    return {f.id for f in report.all_findings if f.severity in severities}


# --------------------------------------------------------------------- tests


def test_fraudulent_relay_is_caught() -> None:
    report = run_audit("fraudulent", FRAUDULENT_MODELS)
    findings = report.all_findings
    ids = {f.id for f in findings}
    counts = report.counts

    print("\n[fraudulent]", {k: v for k, v in counts.items() if v})
    for f in findings:
        if f.severity not in (Severity.CLEAN, Severity.INFO):
            print(f"  {f.severity.value:8} {f.id:22} {f.title}")

    assert counts.get(Severity.CRITICAL.value, 0) >= 1, "掉包行为未被判为 CRITICAL"
    assert "twins-100" in ids, "未检测出双胞胎后端"
    assert "bill-100" in ids, "未检测出隐藏思维链计费"
    assert "canary-100" in ids, "未检测出提示词未透传"
    assert "stream-100" in ids, "未检测出流式与完整返回不一致"
    assert "params-100" in ids, "未检测出参数被静默忽略"

    twins = [f for f in findings if f.id == "twins-100"]
    assert len(twins) >= 2, f"应至少检测出两对双胞胎，实际 {len(twins)}"


def test_fraudulent_relay_reports_markup() -> None:
    report = run_audit("fraudulent", FRAUDULENT_MODELS)
    bill201 = [f for f in report.all_findings if f.id == "bill-201"]
    assert bill201, "未从上流成本中算出加价倍率（_trim 可能又吃掉了标量）"
    margins = bill201[0].evidence.get("per_model_margin")
    assert margins, "bill-201 没有携带加价明细"
    top = max(m["markup_x"] for m in margins)
    assert top > 10, f"加价倍率计算异常：{top}"


def test_params_detects_all_ignored_parameters() -> None:
    report = run_audit("fraudulent", FRAUDULENT_MODELS)
    params = [f for f in report.all_findings if f.id == "params-100"]
    assert params, "未检测出参数被忽略"
    counts = params[0].evidence.get("counts") or {}
    for param in ("max_tokens", "stop", "temperature", "n", "response_format", "tools"):
        assert param in counts, f"参数 {param} 未被判定为忽略：{sorted(counts)}"


def test_clean_relay_produces_no_false_positives() -> None:
    report = run_audit("clean", CLEAN_MODELS)
    noisy = [
        f
        for f in report.all_findings
        if f.severity not in (Severity.CLEAN, Severity.INFO)
    ]

    print("\n[clean]", {k: v for k, v in report.counts.items() if v})
    for f in noisy:
        print(f"  !! {f.severity.value:8} {f.id:22} {f.title}")
        print(f"     {f.summary[:200]}")

    assert not noisy, (
        "诚实中转站被误报："
        + ", ".join(f"{f.id}({f.severity.value})" for f in noisy)
    )


def test_twins_prompts_are_open_ended() -> None:
    """Guard the regression that made every honest relay look fraudulent."""
    from relaycheck.probes.twins import DETERMINISTIC_PROMPTS

    banned = ("1 to 20", "1 to 400", "count from", "compute", "how many", "list exactly")
    for key, prompt in DETERMINISTIC_PROMPTS:
        lowered = prompt.lower()
        for needle in banned:
            assert needle not in lowered, (
                f"提示 {key!r} 含唯一正确答案型问法 {needle!r}；"
                "这种提示会让不同的真实模型输出相同结果，从而产生假阳性。"
            )


def test_endpoint_without_a_billing_panel_is_never_reported_as_clean() -> None:
    """404 means "no panel here" — not "panel reachable and clean".

    An official first-party endpoint (``api.deepseek.com``, ``api.openai.com``)
    has no sub2api panel, no usage API and no model plaza: all six probed paths
    simply 404. ``RelayClient.get_json`` documents "never raises on 4xx", so each
    404 arrived as an ordinary entry carrying ``status: 404`` and no ``error``
    key — while ``reachable`` was computed as "``error`` is not in the entry".
    Every 404 therefore counted as a reachable panel, the report stated "probed 6
    endpoints, 6 reachable", and the probe closed with ``bill-clean``.

    That is the single thing this project must never do: turn "we measured
    nothing" into "verified fine". It also made ``bill-202`` — the honest "all
    panel endpoints unreachable" — unreachable code.

    ``/health`` is exempt from the *verdict* on purpose: it answers 200 on
    almost every deployment, panel or not, so it may be listed as reachable but
    must never license a conclusion about billing.
    """

    report = run_audit("clean", CLEAN_MODELS, probe_names=["billing-panel"], panel=False)
    ids = {f.id for f in report.all_findings}
    print("\n[no-panel]", {k: v for k, v in report.counts.items() if v})
    for f in report.all_findings:
        print(f"  {f.severity.value:8} {f.id:22} {f.title}")

    assert "bill-clean" not in ids, (
        "六个面板端点全部 404，却报出「面板可达」的 bill-clean："
        "把「没测到」写成了「已核实没问题」"
    )
    assert "bill-200" not in ids, "没有任何面板应答，却报出「识别为 sub2api 面板」"
    assert "bill-202" in ids, (
        "面板端点全部 404，既没报 bill-clean 也没报 bill-202，等于把这一项藏了起来"
    )

    probe = next(r for r in report.results if r.probe == "billing-panel")
    assert probe.data["endpoints_reachable"] == ["/health"], (
        "404 被算进了 endpoints_reachable："
        f"{probe.data['endpoints_reachable']!r}（除了 /health 一个都不该有）"
    )


def test_rate_limiting_is_not_reported_as_an_unstable_relay() -> None:
    """429 means "slow down", not "this endpoint is broken".

    An official first-party API rate-limits a probe that fires a trivial request
    every 0.4 s — that is the API working correctly. ``reliability`` used to
    compute one ``failure_rate`` over every non-success and escalate anything
    above ``FAILURE_RATE_HIGH`` (20%) to ``rel-100`` HIGH / 「中转站极不稳定」,
    so pointing relaycheck at an official endpoint would accuse it of instability
    caused entirely by our own pacing.

    The opposite mistake is just as forbidden: with every request refused,
    nothing was measured, and the run must not close with ``rel-clean`` /
    「可用性正常」 either.
    """

    report = run_audit("clean", CLEAN_MODELS, probe_names=["reliability"], throttle=True)
    ids = {f.id for f in report.all_findings}
    print("\n[throttled]", {k: v for k, v in report.counts.items() if v})
    for f in report.all_findings:
        print(f"  {f.severity.value:8} {f.id:22} {f.title}")

    assert report.counts.get("high", 0) == 0, (
        "全部 429 被判成 HIGH：这是拿我们自己的请求节奏去指控一个正在正常限流的端点"
    )
    assert "rel-100" not in ids, "全部 429 被判成 rel-100「中转站极不稳定」"
    assert "rel-clean" not in ids, (
        "一次都没成功，却报出「可用性正常」：把「没测到」写成了「没问题」"
    )
    assert "rel-102" in ids, "限流既没报不稳定也没报未测出，等于把结论吞了"

    stats = next(r for r in report.results if r.probe == "reliability").data["reliability"]
    assert stats["unavailability_rate"] == 0.0, (
        f"429 被算进了不可用率：{stats['unavailability_rate']}"
    )
    assert stats["throttled"] == stats["samples"], (
        f"限流计数对不上：{stats['throttled']} / {stats['samples']}"
    )


def test_slow_relay_does_not_hang_the_audit() -> None:
    """A merely-slow relay must degrade into "incomplete", never into "hung".

    Regression: against a real deployment where roughly half of all requests hit
    a 45-second read timeout, the tokenizer probe ran for more than 30 minutes
    and had to be killed by hand.
    """
    budget = 2.0
    t0 = time.monotonic()
    report = run_audit(
        "slow",
        CLEAN_MODELS,
        options={"probe_budget_s": budget, "reliability_samples": 3},
    )
    elapsed = time.monotonic() - t0

    truncated = [r.probe for r in report.results if r.data.get("truncated")]
    ids = {f.id for f in report.all_findings}
    print("\n[slow]", {k: v for k, v in report.counts.items() if v})
    print(f"  用时 {elapsed:.1f}s，被截断的探针: {truncated}")

    assert elapsed < 60, f"慢中转站把审计拖了 {elapsed:.0f} 秒，探针时间预算没有生效"
    assert truncated, "慢中转站没有产生任何截断标记，说明时间预算没起作用"
    assert "budget-000" in ids, "被截断的探针没有如实上报，会把「没跑完」伪装成「通过」"

    # A slow relay is slow, not fraudulent. It must not be accused of anything
    # above INFO on the strength of our own timeout.
    accusatory = [
        f
        for f in report.all_findings
        if f.severity not in (Severity.CLEAN, Severity.INFO)
    ]
    assert not accusatory, (
        "慢（但诚实）的中转站被误报："
        + ", ".join(f"{f.id}({f.severity.value})" for f in accusatory)
    )


def test_same_vendor_aliases_are_not_accused() -> None:
    """Two names, one backend, same vendor — must not read as substitution.

    Regression: the tokenizer probe flagged *any* two models sharing a tokenizer
    as MEDIUM, and the twins probe flagged shared-tokenizer + identical output as
    HIGH. Both fire on an honest relay that exposes one vendor's lineup, where a
    shared tokenizer is the expected behaviour, not evidence. The distinction
    that matters is whether the *names* claim different vendors.
    """
    report = run_audit("same-vendor", ["gpt-4o", "gpt-4o-mini"])
    ids = {f.id for f in report.all_findings}
    print("\n[same-vendor]", {k: v for k, v in report.counts.items() if v})
    for f in report.all_findings:
        if f.severity not in (Severity.CLEAN, Severity.INFO):
            print(f"  {f.severity.value:8} {f.id:22} {f.title}")

    accusatory = [
        f
        for f in report.all_findings
        if f.severity not in (Severity.CLEAN, Severity.INFO, Severity.LOW)
    ]
    assert not accusatory, (
        "同厂商别名被当成掉包："
        + ", ".join(f"{f.id}({f.severity.value})" for f in accusatory)
    )

    assert "tok-100" not in ids, "同厂商共享 tokenizer 被误判为掉包证据（tok-100）"
    assert "tok-101" in ids, "同厂商共享 tokenizer 未按「符合预期」如实记录（tok-101）"
    assert "twins-100" not in ids, "同厂商别名被误判为双胞胎后端（twins-100）"
    assert "twins-101" in ids, "同厂商别名未被如实记录为低可信度观察（twins-101）"


def test_dead_relay_is_never_reported_as_clean() -> None:
    """A relay that answers nothing must not come back "clean".

    Regression: the identity and canary probes treated an empty result set as
    "no contradiction found" and emitted a CLEAN finding with CONFIRMED
    confidence. On a dead endpoint nothing had been checked at all, so the
    report said "prompt forwarding is fine" about a relay that never replied.
    A CLEAN verdict has to mean *verified*, otherwise the whole report is
    worthless: the one case where you most need the tool to speak up is the one
    where it says nothing is wrong.
    """
    report = run_audit("dead", CLEAN_MODELS)
    ids = {f.id for f in report.all_findings}
    print("\n[dead]", {k: v for k, v in report.counts.items() if v})
    for f in report.all_findings:
        print(f"  {f.severity.value:8} {f.id:22} {f.title}")

    # These are the exact ids that used to fire on a dead endpoint. Each one is
    # a CLEAN verdict for a check that never ran. ``ctx-clean`` is the newest
    # member: it is only allowed out when a depth was genuinely retrieved.
    wrongly_clean = [
        i
        for i in (
            "id-clean",
            "canary-clean",
            "stream-clean",
            "params-clean",
            "twins-clean",
            "ctx-clean",
        )
        if i in ids
    ]
    assert not wrongly_clean, (
        "没有任何东西被检验，却给出了 CLEAN 判决：" + ", ".join(wrongly_clean)
    )

    # The probes that cannot measure must say so explicitly.
    assert "canary-000" in ids, "标记回显检查全失败，却没有报告「未检验」（canary-000）"
    assert "id-000" in ids, "自述采集全失败，却没有报告「未检验」（id-000）"
    assert "stream-000" in ids, "流式比对全失败，却没有报告「未检验」（stream-000）"
    assert "ctx-000" in ids, "长输入检查全失败，却没有报告「未检验」（ctx-000）"
    # And the availability probe must name the real problem.
    assert "rel-100" in ids, "全站不可用，可用性探针没有报 HIGH（rel-100）"


def test_context_probe_catches_silent_truncation() -> None:
    """Long inputs must be provably delivered — or the relay must be called out.

    Regression: nothing in the audit used to depend on the *size* of the input.
    Every other probe here sends a few hundred characters, so a relay could
    advertise a large context, forward only the last few thousand characters to
    the backend, bill the whole prompt, and still come back clean. That is the
    failure this probe exists for: the answer is fluent, the usage block is
    plausible, and nothing in the response ever mentions what was dropped.
    """
    bad = run_audit("fraudulent", ["gpt-4o", "gemini-1.5-pro"], probe_names=["context"])
    ids = {f.id for f in bad.all_findings}
    print("\n[context/fraudulent]", {k: v for k, v in bad.counts.items() if v})
    for f in bad.all_findings:
        print(f"  {f.severity.value:8} {f.id:22} {f.title}")

    ctx100 = [f for f in bad.all_findings if f.id == "ctx-100"]
    assert ctx100, "长输入被静默截断，却没有报告（ctx-100）"
    assert ctx100[0].severity is Severity.HIGH, (
        f"有更浅的对照深度时，静默截断只报到了 {ctx100[0].severity.value}；"
        "对照深度通过、更深一档只剩文末标记，这是可以直接观察到的行为。"
    )
    case = ctx100[0].evidence["cases"][0]
    assert case["missing"] == "head", f"截断方向记录错误：missing={case['missing']!r}"
    assert case["depths_that_passed"], "更浅的一档本应完整通过，证据里却没有记录"
    assert case["actual_prompt_tokens"], "证据里没有服务端回报的 prompt_tokens"

    # The other half: on an honest relay the same check has to actually verify,
    # not fall back to "could not tell". Otherwise the probe could rot into a
    # permanent INFO and every clean report would be silently meaningless here.
    honest = run_audit("clean", ["gpt-4o"], probe_names=["context"])
    honest_ids = {f.id for f in honest.all_findings}
    print("[context/clean]", {k: v for k, v in honest.counts.items() if v})
    noisy = [
        f
        for f in honest.all_findings
        if f.severity not in (Severity.CLEAN, Severity.INFO)
    ]
    assert not noisy, "诚实中转站的长输入被误报：" + ", ".join(f.id for f in noisy)
    assert "ctx-clean" in honest_ids, (
        "诚实中转站的长输入没有被真正验证过（应为 ctx-clean）；"
        "说明这一项其实是不确定的，而不是通过的。"
    )


# ----------------------------------------------------------------- standalone


def test_reasoning_relay_is_not_falsely_accused() -> None:
    """An empty answer is not evidence. A reasoning backend must not be accused.

    Regression, found against a real and honest station: its two models are
    DeepSeek-style reasoning backends that spend their first N tokens on hidden
    ``reasoning_content`` and only then start writing ``content``. Every probe
    used to ask for a small ``max_tokens`` (twins 200, temperature samples 120,
    stream 300), so what came back was nothing but reasoning and an empty
    string — and relaycheck compared two empty strings, found them "different",
    and reported HIGH model substitution plus MEDIUM "temperature is accepted
    and ignored". Both were our own bug, and both amounted to accusing an honest
    relay of fraud on the strength of a check that measured nothing.

    This is the mirror image of the dead-relay rule: there, a missing answer was
    laundered into CLEAN; here, a missing answer was inflated into an
    accusation. Neither is a measurement.
    """

    report = run_audit("reasoning", CLEAN_MODELS)
    ids = {f.id for f in report.all_findings}
    print("\n[reasoning]", {k: v for k, v in report.counts.items() if v})
    for f in report.all_findings:
        print(f"  {f.severity.value:8} {f.id:22} {f.title}")

    noisy = [
        f
        for f in report.all_findings
        if f.severity not in (Severity.CLEAN, Severity.INFO)
    ]
    assert not noisy, (
        "推理模型后端造成的空回复被当成了证据："
        + ", ".join(f"{f.id}({f.severity.value})" for f in noisy)
    )

    # The two exact false positives this scenario was written to pin down.
    assert "stream-100" not in ids, (
        "流式与完整两条路径都返回空字符串，却被判为「内容不一致」（stream-100）"
    )
    assert "params-100" not in ids, (
        "四个温度样本全是空字符串，却被判为「参数被接受但忽略」（params-100）"
    )

    # Honest-but-useless is not good enough either: with room to think, the
    # probes have to actually reach a verdict rather than park on "unknown".
    assert "twins-clean" in ids, (
        "推理模型在放大 token 预算后可以正常作答，双胞胎比对却仍是「未能进行」；"
        "说明自动重试没有生效。"
    )
    assert "id-clean" in ids, "推理模型在放大 token 预算后可以正常自述，身份检查却没有结论"
    assert "canary-clean" in ids, "标记回显检查在放大 token 预算后仍未完成"


def test_reasoning_that_outlives_the_retry_budget_is_not_an_accusation() -> None:
    """When our own token cap is why there is nothing to read, say exactly that.

    ``ctx.ask`` retries a starved answer once with ``max(60 * 4, 1024) == 1024``
    tokens (``base.grow_budget``). A thinking model can burn even that, and then
    *every* attempt comes back with an empty ``content`` under
    ``finish_reason == "length"``.

    The canary check used to record ``canary in comp.content`` for that empty
    string — False — and escalate to ``canary-100`` HIGH, telling the user their
    prompt had probably been rewritten or answered from cache, on the strength of
    a measurement our own ``max_tokens`` had destroyed. The stock ``reasoning``
    scenario never caught it because ``_REASONING_TOKENS = 700`` sits *below* the
    1024 retry ceiling, so the retry always recovered and the bug stayed hidden.

    The identity check had the mirror bug in the other direction: an empty answer
    counted as a usable self-description (``"".startswith("<")`` is False), so the
    probe printed ``id-clean`` — "N models described themselves and none
    contradicted the sold name" — about models that had said nothing at all.

    Both are the dead-relay rule pointing the other way: an unmeasured check
    reports that it could not measure, and never reports CLEAN *or* a finding.
    """

    report = run_audit(
        "clean",
        CLEAN_MODELS,
        probe_names=["identity", "canary"],
        reasoning_tokens=4096,
    )
    ids = {f.id for f in report.all_findings}
    print("\n[starved]", {k: v for k, v in report.counts.items() if v})
    for f in report.all_findings:
        print(f"  {f.severity.value:8} {f.id:22} {f.title}")

    noisy = [
        f
        for f in report.all_findings
        if f.severity not in (Severity.CLEAN, Severity.INFO)
    ]
    assert not noisy, (
        "被我们自己设的 token 上限挤空的回复被当成了证据："
        + ", ".join(f"{f.id}({f.severity.value})" for f in noisy)
    )

    assert "canary-100" not in ids, (
        "模型一次都没复述标记，是因为可见正文被 token 上限吃空了，"
        "而不是因为中转站改写了提示词：canary-100 是拿自己的 max_tokens 当证据"
    )
    assert "id-clean" not in ids, (
        "所有自述都是空字符串，却判成「自述与售卖名称不矛盾」（id-clean）"
    )
    assert "canary-clean" not in ids, "一次都没回显成功，却报了 canary-clean"

    # The honest half of the rule: it has to *say* it could not check.
    assert "canary-000" in ids, (
        "标记回显一次都没测成，既没报 canary-100 也没报 canary-000，"
        "等于把「没测到」藏起来了"
    )
    assert "id-000" in ids, (
        "一份自述都没采到，却既没报 id-100 也没报 id-000"
    )


def test_noisy_relay_is_not_falsely_accused() -> None:
    """An unreproducible station is not evidence of anything either.

    Regression, found against the same real and honest station as the reasoning
    case, one round later. Its backend does not reproduce its own output even at
    ``temperature=0``: ask twice, get two different paragraphs. That is ordinary
    behaviour for a batched or mixture-of-experts backend, and it broke two
    probes at once:

    * ``stream`` compared the streamed answer against a *single* plain answer and
      called the wording difference a HIGH-severity model swap;
    * ``params`` read "two calls at temperature=0 differed" as "the parameter was
      dropped" and called it MEDIUM.

    Both inferences are only valid once the station has been shown to reproduce
    itself. This scenario pins the corrected behaviour: measure the noise floor
    first, compare on the deterministic question instead, and when even that is
    not available say so instead of guessing.
    """

    report = run_audit("noisy", CLEAN_MODELS)
    ids = {f.id for f in report.all_findings}
    print("\n[noisy]", {k: v for k, v in report.counts.items() if v})
    for f in report.all_findings:
        print(f"  {f.severity.value:8} {f.id:22} {f.title}")

    noisy = [
        f
        for f in report.all_findings
        if f.severity not in (Severity.CLEAN, Severity.INFO)
    ]
    assert not noisy, (
        "不可复现的诚实后端被当成了证据："
        + ", ".join(f"{f.id}({f.severity.value})" for f in noisy)
    )

    # The two exact false positives this scenario was written to pin down.
    assert "stream-100" not in ids, (
        "该站在 temperature=0 下都不可复现，流式与完整的措辞差异却被判为「内容不一致」"
    )
    assert "params-100" not in ids, (
        "temperature=0 的两次调用结果不同，却被判为「参数被接受但忽略」"
    )

    # The honest verdicts. Silence would be its own kind of lie here: the station
    # does not reproduce itself, and the report has to say so somewhere.
    assert "stream-104" in ids, (
        "无法进行流式比对时没有如实报告，读者会以为两条路径已经比对过"
    )
    assert "params-104" in ids, (
        "无法验证采样参数时没有如实报告"
    )
    assert "stream-clean" not in ids, (
        "开放式比对因不可复现而被弃用，报告却宣称「流式与完整返回内容一致」——"
        "把一次没有真正执行的比对写成了通过"
    )


def test_cli_survives_a_legacy_console_encoding() -> None:
    """A console code page must never be able to fake a verdict.

    On a Chinese Windows install ``sys.stdout`` is cp936. The progress line used a
    glyph that code page lacks (``✓``), which raised ``UnicodeEncodeError``
    **inside the probe loop**: the audit died before writing any report, and the
    traceback left the process with status 1 — the exact code this tool reserves
    for "findings at or above ``--fail-on``". Read from a shell, a cosmetics bug
    was indistinguishable from a positive result.

    This runs the real CLI in a child process pinned to cp936 and asserts that it
    finishes, writes its report, and exits 0 (the mock relay is clean, so 0 is
    the correct verdict — 1 would be a crash wearing a verdict's clothes).
    """
    import os
    import subprocess
    import tempfile

    server = _Server("clean")
    tmp = tempfile.mkdtemp(prefix="relaycheck-encoding-")
    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "gbk"
        env.pop("RELAYCHECK_API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
        root = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "relaycheck.cli",
                "-u",
                server.base_url,
                "-k",
                mock_relay.TEST_API_KEY,
                "--models",
                "gpt-4o",
                "--probes",
                "reliability",
                "--delay",
                "0",
                "--out-dir",
                tmp,
            ],
            cwd=str(root),
            env=env,
            capture_output=True,
            timeout=180,
        )
        stderr = proc.stderr.decode("gbk", "replace")
        assert "Traceback" not in stderr, f"CLI 在 cp936 控制台下抛异常：\n{stderr}"
        assert proc.returncode == 0, (
            f"CLI 在 cp936 控制台下没有正常收尾：exit={proc.returncode}"
            f"（1 意味着「发现了问题」，而不是「崩了」）\n{stderr}"
        )
        assert (Path(tmp) / "report.md").exists(), "CLI 在 cp936 控制台下没有写出报告"
    finally:
        server.close()


def test_echo_probe_compares_the_reported_model_name() -> None:
    """The ``model`` field of a response is filled in by whoever answered.

    A gateway that fronts several sales names with one backend has to put
    *something* in that field, and the usual something is the name of the model it
    actually called — rewriting it is extra work and no client checks it. That
    makes this the cheapest hard evidence in the tool: two requests per model, no
    prompt engineering, no statistics. It is also checked on both paths, because
    the plain and the streaming response are two separate places where the same
    field has to be filled in.
    """

    report = run_audit("fraudulent", FRAUDULENT_MODELS, probe_names=["echo"])
    ids = {f.id for f in report.all_findings}
    print("\n[echo/fraudulent]", sorted(ids))
    assert "echo-100" in ids, sorted(ids)

    finding = next(f for f in report.all_findings if f.id == "echo-100")
    assert finding.severity is Severity.MEDIUM, finding.severity
    assert finding.confidence is Confidence.LIKELY, finding.confidence
    reported = {o["model_requested"] for o in finding.evidence["cross_vendor"]}
    assert reported == {"gemini-1.5-pro", "deepseek-chat"}, reported
    assert {o["path"] for o in finding.evidence["cross_vendor"]} == {"plain", "stream"}

    # An honest relay names itself, on both paths, for every model.
    honest = run_audit("clean", CLEAN_MODELS, probe_names=["echo"])
    honest_ids = {f.id for f in honest.all_findings}
    print("[echo/clean]", sorted(honest_ids))
    assert honest_ids == {"echo-clean"}, sorted(honest_ids)

    # Two names for one same-vendor backend is a legitimate alias pair, not a
    # substitution — this is the ``same-vendor`` trap again, from a new angle.
    aliases = run_audit("same-vendor", ["gpt-4o", "gpt-4o-mini"], probe_names=["echo"])
    alias_ids = {f.id for f in aliases.all_findings}
    print("[echo/same-vendor]", sorted(alias_ids))
    assert "echo-100" not in alias_ids, "同一后端挂两个同厂商名字被当成了掉包"
    assert "echo-101" in alias_ids, sorted(alias_ids)


def test_unstable_self_report_is_not_an_accusation() -> None:
    """A witness that contradicts itself is not a witness.

    Regression from a real honest station: asked which company made it, the model
    answered "anthropic" on one round and "openai" on the next. Neither answer is
    a fact — for a thinking model the first reply to that question is a sample
    from a distribution over plausible origins, which is exactly why so many
    non-OpenAI models say "OpenAI". The probe used to believe the first sample and
    print a substitution lead from it.

    Now the lead has to survive being asked twice. When it does not, the report
    must say so: no ``id-100`` (the lead did not hold), and no ``id-clean`` either
    (the question was never settled, and a clean line here would imply it was).
    """

    report = run_audit("unstable-self", ["gpt-4o"], probe_names=["identity"])
    ids = {f.id for f in report.all_findings}
    print("\n[unstable-self]", {k: v for k, v in report.counts.items() if v})
    for f in report.all_findings:
        print(f"  {f.severity.value:8} {f.id:22} {f.title}")

    above = [
        f
        for f in report.all_findings
        if f.severity not in (Severity.CLEAN, Severity.INFO)
    ]
    assert not above, "自述不稳定却被当成了指控：" + ", ".join(
        f"{f.id}({f.severity.value})" for f in above
    )

    assert "id-100" not in ids, "两遍自述互相矛盾，仍然发出了「自称与售卖名称不一致」"
    assert "id-101" in ids, sorted(ids)
    assert "id-clean" not in ids, "自述两次不一致，却报告了「未发现矛盾」"

    finding = next(f for f in report.all_findings if f.id == "id-101")
    unstable = finding.evidence["unstable_self_reports"]
    assert unstable, "id-101 没有给出任何不稳定自述的证据"
    entries = unstable[0]["unstable_self_reports"]
    assert entries, unstable[0]
    assert entries[0]["first"] != entries[0]["second"], entries[0]
    assert entries[0]["named_family"] == ["anthropic"], entries[0]


def test_model_autodiscovery_runs_without_models_flag() -> None:
    """``--models`` is optional, and the branch that omits it had no test.

    Every other check in this file builds its own ``ProbeContext`` with an
    explicit model list, because those checks are about probe behaviour. That
    left the path a first-time user actually takes — no ``--models``, models
    discovered from ``/v1/models`` — never executed by the suite. Its failure
    mode is quiet: the wrong models get picked, the audit still finishes, and the
    report still prints a verdict.

    The mock advertises three models from three different vendors, so asking for
    two slots must return one from each rather than the first two catalogue
    entries. Asserted on the written ``report.json`` (the machine artefact) and
    on the progress line (what the user sees).
    """
    import json
    import os
    import subprocess
    import tempfile

    server = _Server("clean")
    tmp = tempfile.mkdtemp(prefix="relaycheck-autodiscovery-")
    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("RELAYCHECK_API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
        root = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "relaycheck.cli",
                "-u",
                server.base_url,
                "-k",
                mock_relay.TEST_API_KEY,
                # No --models: that omission is the whole point of this check.
                "--max-models",
                "2",
                "--probes",
                "reliability",
                "--reliability-samples",
                "2",
                "--delay",
                "0",
                "--out-dir",
                tmp,
            ],
            cwd=str(root),
            env=env,
            capture_output=True,
            timeout=180,
        )
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        assert "Traceback" not in stderr, f"自动发现模型时 CLI 抛异常：\n{stderr}"
        assert proc.returncode == 0, (
            f"自动发现模型时 CLI 没有正常收尾：exit={proc.returncode}\n{stderr}\n{stdout}"
        )

        expected = ["gpt-4o", "claude-3-5-sonnet"]
        report = json.loads((Path(tmp) / "report.json").read_text(encoding="utf-8"))
        assert report["models_tested"] == expected, report["models_tested"]
        assert report["models_available_count"] == len(mock_relay.CLEAN_MODELS), (
            "可用模型数不是 /v1/models 返回的个数："
            f"{report['models_available_count']} != {len(mock_relay.CLEAN_MODELS)}"
        )
        assert "受测 2 个: gpt-4o[openai], claude-3-5-sonnet[anthropic]" in stdout, (
            f"进度行没有报出实际选择：\n{stdout}"
        )
    finally:
        server.close()


def test_a_refusal_to_list_a_marker_is_not_a_truncated_prefix() -> None:
    """A model saying "I only see one" is talking, not listing.

    Regression: ``_classify`` decided ``truncated`` from ``head in text`` alone.
    A long-context model handed more than it can hold answers "I can only see
    one of the two codes" and quotes the one it got while naming the other as
    absent — and the substring test read that as "the tail survived, the head
    did not", i.e. as proof that the front of the prompt was dropped before it
    ever reached the model. The report then accused the relay of silent
    truncation (HIGH) on the strength of a polite refusal.
    """
    from relaycheck.models import Completion, Usage
    from relaycheck.probes.context import _classify

    head, tail = "ALPHA-ABCDEFGH2345", "BETA-ZYXWVUTS6789"

    def classify(text: str) -> dict:
        return _classify(
            Completion(
                model_requested="m",
                model_returned="m",
                content=text,
                usage=Usage(),
                finish_reason="stop",
            ),
            head,
            tail,
            4000,
            16000,
        )

    refusal = classify(
        f"I can only see one of the two codes: {tail}. The other one is missing."
    )
    assert refusal["verdict"] != "truncated", (
        "把「我只看到一个标记」读成了「前段输入被丢弃」——拿礼貌回绝当 HIGH 证据"
    )
    assert "missing" not in refusal

    # The real case still has to work: only the codes, one of them absent.
    real = classify(tail)
    assert real["verdict"] == "truncated", "真正的截断没有被认出来"
    assert real["missing"] == "head"


def test_reproducibility_requires_an_identical_answer() -> None:
    """A 90%-prefix match is not a noise floor.

    ``_compare_answers`` tolerates a shortened tail so that a stream cut off at
    the finish still counts as the same answer. Used as the *reproducibility*
    gate, that tolerance promoted two merely similar ``temperature=0`` samples
    into "this station reproduces itself", after which ordinary sampling noise
    was reported as a stream/plain mismatch — a HIGH-severity false positive.
    """
    from relaycheck.probes.stream import _reproduces_itself

    assert _reproduces_itself("abc def", "abc def")
    assert _reproduces_itself("abc   def", "abc def")  # whitespace only
    assert not _reproduces_itself("abc def ghi", "abc def")
    assert not _reproduces_itself("", "")


def test_a_single_model_endpoint_is_not_a_crashed_probe() -> None:
    """One model is a hole in the audit, not a broken tool.

    Regression: ``twins.run`` set ``result.error = "需要至少 2 个模型…"`` and
    returned. The reporter renders any ``result.error`` as 「探针崩溃」 and the CLI
    counts it as a failed probe, so an official endpoint that publishes exactly
    one model — ``api.deepseek.com`` in a quiet moment, any single-model vendor —
    looked like relaycheck itself had fallen over. The reader got a crash notice
    instead of the one fact that mattered: the twin check never ran.
    """
    report = run_audit("clean", ["gpt-4o"], probe_names=["twins"])
    ids = {f.id for f in report.all_findings}
    print("\n[one-model]", {k: v for k, v in report.counts.items() if v}, sorted(ids))

    result = next(r for r in report.results if r.probe == "twins")
    assert result.error is None, f"单模型端点被渲染成探针崩溃：{result.error!r}"
    assert "twins-000" in ids, "单模型端点没有如实上报「双胞胎比对未能进行」（twins-000）"
    assert "twins-clean" not in ids, "没有第二个可比方，却给出了「未发现同一后端」的 CLEAN"

    accusatory = [
        f for f in report.all_findings if f.severity not in (Severity.CLEAN, Severity.INFO)
    ]
    assert not accusatory, "单模型端点被误报：" + ", ".join(
        f"{f.id}({f.severity.value})" for f in accusatory
    )


def test_same_vendor_aliases_survive_without_tokenizer_data() -> None:
    """A one-vendor alias pair is LOW even when nothing corroborates it.

    Regression: the final ``else`` in ``twins.run`` swallowed every pair that was
    neither cross-vendor nor tokenizer-matched. ``tokenizer_groups`` is empty
    whenever the tokenizer probe did not run (``--probes twins``), failed, or the
    upstream never reported ``prompt_tokens`` — so an honest relay selling one
    vendor's lineup collected a MEDIUM 「两个模型的输出完全一致」 for a legitimate
    alias pair, with the single piece of evidence that would have cleared it
    simply never collected.
    """
    report = run_audit("same-vendor", ["gpt-4o", "gpt-4o-mini"], probe_names=["twins"])
    ids = {f.id for f in report.all_findings}
    print("\n[same-vendor/twins-only]", {k: v for k, v in report.counts.items() if v})

    probe = next(r for r in report.results if r.probe == "twins")
    assert probe.data.get("tokenizer_groups_considered") == [], (
        "这条测试的前提是没有任何 tokenizer 旁证，实际拿到了 "
        f"{probe.data.get('tokenizer_groups_considered')!r}"
    )

    assert "twins-100" not in ids, "同厂商别名在缺 tokenizer 旁证时被当成掉包（twins-100）"
    assert "twins-101" in ids, "同厂商别名未被如实记录为低可信度观察（twins-101）"

    accusatory = [
        f
        for f in report.all_findings
        if f.severity not in (Severity.CLEAN, Severity.INFO, Severity.LOW)
    ]
    assert not accusatory, "缺 tokenizer 旁证时同厂商别名仍被误报：" + ", ".join(
        f"{f.id}({f.severity.value})" for f in accusatory
    )


def test_missing_usage_is_not_an_accusation() -> None:
    """A gateway that omits ``usage`` is a compatibility gap, not a swap.

    Regression: ``tok-001`` fired at MEDIUM whenever one model reported
    ``prompt_tokens`` and another did not. Gateways that speak Anthropic-shaped
    usage — and gateways that drop usage from streamed responses — hit that while
    running the real model, and the report turned "this model's fingerprint could
    not be measured" into an accusation of substitution.
    """
    report = run_audit(
        "clean",
        ["gpt-4o", "claude-3-5-sonnet", "deepseek-chat"],
        probe_names=["tokenizer"],
        strip_usage_for=("deepseek-chat",),
    )
    ids = {f.id for f in report.all_findings}
    print("\n[no-usage]", {k: v for k, v in report.counts.items() if v}, sorted(ids))

    tok001 = [f for f in report.all_findings if f.id == "tok-001"]
    assert tok001, "有模型缺 usage、其余正常，却没有报出这件事（tok-001 缺失）"
    assert tok001[0].severity is Severity.INFO, (
        f"缺 usage 被报到了 {tok001[0].severity.value}；这只是测不出来，不是掉包的证据"
    )

    accusatory = [
        f for f in report.all_findings if f.severity not in (Severity.CLEAN, Severity.INFO)
    ]
    assert not accusatory, "缺 usage 的中转站被误报：" + ", ".join(
        f"{f.id}({f.severity.value})" for f in accusatory
    )


def test_a_starved_billing_sample_is_not_reported_as_clean() -> None:
    """A billing sample the budget emptied must not be cleared as "checked, fine".

    Two halves of one mistake in ``billing.py``:
      * the probe called ``ctx.client.chat`` directly, so the grow-retry in
        ``base.py`` never ran and a reasoning backend's empty answer was taken
        as the measurement;
      * an empty answer gives ``billed == 0`` → ``expected == 0`` → the
        ``expected > 0`` guard fell through to ``verdict = "ok"``, i.e. "measured
        and normal", on a completion the probe never saw.

    A relay that hides its reasoning behind a budget large enough to survive the
    retry is exactly the shape this probe exists to catch, so clearing it would
    clear the one case that matters most.
    """
    report = run_audit(
        "clean",
        CLEAN_MODELS,
        probe_names=["billing-hidden-reasoning"],
        reasoning_tokens=4096,
    )
    ids = {f.id for f in report.all_findings}
    print("\n[starved-billing]", {k: v for k, v in report.counts.items() if v}, sorted(ids))

    assert "bill-clean" not in ids, (
        "billing 探针一个可见回答都没拿到，却报出「未发现隐藏 reasoning 计费」"
    )
    assert "bill-100" not in ids, "没有正文可看，却指控隐藏 reasoning 计费"

    bill000 = [f for f in report.all_findings if f.id == "bill-000"]
    assert bill000, "这一项没被检验，却也没写出来（bill-000 缺失）"
    assert bill000[0].severity is Severity.INFO, (
        f"「测不出来」被报到了 {bill000[0].severity.value}"
    )

    # The observable half of C4: the per-model verdict used to read ``ok``,
    # i.e. "measured and normal", directly beside ``billed_vs_estimated_ratio:
    # None`` — the record contradicted itself.
    probe = next(r for r in report.results if r.probe == "billing-hidden-reasoning")
    verdicts = {
        model: obs.get("verdict")
        for model, obs in (probe.data.get("observations") or {}).items()
    }
    assert set(verdicts.values()) == {"unmeasurable"}, (
        f"没有可见正文的样本被判成 {verdicts}，而不是「测不出来」"
    )


def test_the_billing_probe_grows_the_budget_before_giving_up() -> None:
    """The billing probe must reuse ``ctx.ask``, not call the client directly.

    Regression (C5): ``billing.py`` called ``ctx.client.chat`` with a fixed
    ``max_tokens=512``. A reasoning backend that spends 1500 tokens thinking is
    starved at 512 but answers fine at 2048 — the grow-retry in ``base.py``
    covers exactly that gap, and bypassing it threw the sample away. Every other
    probe already went through ``ctx.ask``.
    """
    report = run_audit(
        "clean",
        CLEAN_MODELS,
        probe_names=["billing-hidden-reasoning"],
        reasoning_tokens=1500,
    )
    ids = {f.id for f in report.all_findings}
    print("\n[grown-billing]", {k: v for k, v in report.counts.items() if v}, sorted(ids))

    assert "bill-000" not in ids, (
        "reasoning 花掉 1500 tokens：重试到 2048 就能拿到回答，却报了「没能检测」"
    )
    assert "bill-clean" in ids, "拿到了可用样本，却没有给出任何结论"
    assert "bill-100" not in ids, "短回答下正常计费，却被指控隐藏 reasoning"


def test_stop_is_not_called_ignored_when_there_is_nothing_to_compare() -> None:
    """``stop`` may only be judged from a comparison that actually happened.

    Regression: ``_check_stop`` computed
    ``honoured = _STOP_TOKEN not in stopped and len(stopped) < len(plain)``. An
    empty answer satisfies both halves for free — the token is trivially absent,
    and ``0 < len(plain)`` — so a response the budget emptied was recorded as
    "stop honoured", and ``params-clean`` then cleared a parameter that was
    never exercised.

    The note was wrong in the other direction as well: it was emitted
    unconditionally, so the ``honoured`` branch said "stop was silently dropped"
    — a finding that contradicted its own verdict.
    """
    from types import SimpleNamespace

    from relaycheck.probes.params import ParamsProbe

    probe = ParamsProbe()
    plain_answer = "0 1 2 3 4 5 6 7 8 9"

    def outcome(*answers: str) -> dict:
        pending = iter(answers)

        class _Ctx:
            def ask(self, model, messages, **kwargs):  # noqa: ANN001
                return SimpleNamespace(content=next(pending))

        return probe._check_stop(_Ctx(), "gpt-4o")  # type: ignore[arg-type]

    emptied = outcome(plain_answer, "")
    assert emptied["verdict"] == "inconclusive", (
        f"没有返回内容却判成 {emptied['verdict']}，「没测到」被写成了结论"
    )

    honoured = outcome(plain_answer, "0 1 2 3 4")
    assert honoured["verdict"] == "honoured", honoured
    assert "静默丢弃" not in honoured["note"], (
        "verdict 是 honoured，说明文字却说 stop 被静默丢弃"
    )

    ignored = outcome(plain_answer, plain_answer)
    assert ignored["verdict"] == "ignored", ignored
    assert "静默丢弃" in ignored["note"]


def test_a_throttled_run_is_not_reported_as_a_slow_relay() -> None:
    """A median latency measured while being throttled is not the endpoint's speed.

    Regression (``rel-101``): the verdict chain checked ``p50_latency_s >
    SLOW_P50_S`` *before* it looked at 429/4xx evidence. A run that is only
    partly refused still has successes, so a median exists — and that median
    carries the time the endpoint made us wait in its rate-limit queue. The
    finding then read 「上游是共享池、排队严重或者被限速，而不是直连官方 API」,
    which is exactly what an official first-party endpoint looks like under a
    0.4 s probe cadence.

    ``SLOW_P50_S`` is lowered here so a sub-second mock can stand in for a real
    queueing delay — the branch ordering is what is under test, not the constant.
    """
    import relaycheck.probes.reliability as reliability_mod

    original = reliability_mod.SLOW_P50_S
    reliability_mod.SLOW_P50_S = -1.0
    try:
        report = run_audit(
            "clean",
            CLEAN_MODELS,
            probe_names=["reliability"],
            options={"reliability_samples": 6},
            throttle_every=2,
        )
    finally:
        reliability_mod.SLOW_P50_S = original

    ids = {f.id for f in report.all_findings}
    probe = next(r for r in report.results if r.probe == "reliability")
    stats = probe.data.get("reliability") or probe.data
    print("\n[throttled-slow]", {k: v for k, v in report.counts.items() if v}, sorted(ids), stats)

    assert stats.get("throttled"), (
        f"测试前提不成立：这一轮没有被限流的样本，stats={stats}"
    )
    assert stats.get("succeeded"), "测试前提不成立：这一轮一次都没成功，没有延迟中位数可谈"
    assert "rel-101" not in ids, (
        "一半样本被 429 回绝，仍把混了排队时间的中位延迟当成端点自己的速度去指控"
    )
    assert "rel-102" in ids, "被限流这件事没有照实记下来"
    accusatory = [
        f for f in report.all_findings if f.severity not in (Severity.CLEAN, Severity.INFO)
    ]
    assert not accusatory, "被限流的诚实端点被误报：" + ", ".join(
        f"{f.id}({f.severity.value})" for f in accusatory
    )


def _main() -> int:
    checks = [
        test_model_autodiscovery_runs_without_models_flag,
        test_fraudulent_relay_is_caught,
        test_fraudulent_relay_reports_markup,
        test_params_detects_all_ignored_parameters,
        test_clean_relay_produces_no_false_positives,
        test_twins_prompts_are_open_ended,
        test_endpoint_without_a_billing_panel_is_never_reported_as_clean,
        test_rate_limiting_is_not_reported_as_an_unstable_relay,
        test_a_throttled_run_is_not_reported_as_a_slow_relay,
        test_slow_relay_does_not_hang_the_audit,
        test_same_vendor_aliases_are_not_accused,
        test_same_vendor_aliases_survive_without_tokenizer_data,
        test_missing_usage_is_not_an_accusation,
        test_a_starved_billing_sample_is_not_reported_as_clean,
        test_the_billing_probe_grows_the_budget_before_giving_up,
        test_stop_is_not_called_ignored_when_there_is_nothing_to_compare,
        test_dead_relay_is_never_reported_as_clean,
        test_context_probe_catches_silent_truncation,
        test_a_refusal_to_list_a_marker_is_not_a_truncated_prefix,
        test_reproducibility_requires_an_identical_answer,
        test_reasoning_relay_is_not_falsely_accused,
        test_reasoning_that_outlives_the_retry_budget_is_not_an_accusation,
        test_noisy_relay_is_not_falsely_accused,
        test_a_single_model_endpoint_is_not_a_crashed_probe,
        test_echo_probe_compares_the_reported_model_name,
        test_unstable_self_report_is_not_an_accusation,
        test_cli_survives_a_legacy_console_encoding,
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
