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
from relaycheck.reporter import Report  # noqa: E402

FRAUDULENT_MODELS = ["gpt-4o", "gemini-1.5-pro", "claude-3-5-sonnet", "deepseek-chat"]
CLEAN_MODELS = ["gpt-4o", "claude-3-5-sonnet", "deepseek-chat"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Server:
    def __init__(self, scenario: str) -> None:
        self.port = _free_port()
        self.httpd = mock_relay.serve(self.port, scenario)
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
) -> Report:
    server = _Server(scenario)
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


def _main() -> int:
    checks = [
        test_fraudulent_relay_is_caught,
        test_fraudulent_relay_reports_markup,
        test_params_detects_all_ignored_parameters,
        test_clean_relay_produces_no_false_positives,
        test_twins_prompts_are_open_ended,
        test_slow_relay_does_not_hang_the_audit,
        test_same_vendor_aliases_are_not_accused,
        test_dead_relay_is_never_reported_as_clean,
        test_context_probe_catches_silent_truncation,
        test_reasoning_relay_is_not_falsely_accused,
        test_noisy_relay_is_not_falsely_accused,
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
