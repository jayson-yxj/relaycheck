"""relaycheck command-line interface.

Everything the tool does is read-only: it sends chat completions and reads
public/self-scoped panel endpoints. It never creates accounts, never redeems
anything, and never modifies remote state.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlparse

from . import __version__
from .client import DEFAULT_BROWSER_UA, RelayClient, RelayError
from .models import SEVERITY_ORDER, Severity
from .probes import ProbeContext, probe_names, run_probes, select_probes
from .reporter import Report, render_text, utc_now_iso
from .selection import describe_selection, select_models

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

_EPILOG = """\
使用示例
--------
最简用法（自动发现模型，跑默认探针）:

  relaycheck --base-url https://api.example.com --api-key sk-xxxx

指定要对比的模型（双胞胎检测最有价值，尽量选声称来自不同厂商的）:

  relaycheck -u https://api.example.com -k sk-xxxx \\
      --models "gpt-4o,claude-3-5-sonnet,deepseek-chat,gemini-1.5-pro"

全量探针（含参数透传与流式完整性，请求数更多）:

  relaycheck -u https://api.example.com -k sk-xxxx --probes all

只做 tokenizer 指纹（最便宜、最硬的证据）:

  relaycheck -u https://api.example.com -k sk-xxxx --probes tokenizer,twins

检查长输入是否被悄悄截断（请求数少但每次都很贵，按需使用）:

  relaycheck -u https://api.example.com -k sk-xxxx --probes context

输出
----
每次运行都会在 --out-dir 下产出:
  report.md    可读报告（可直接发给商家或作为投诉附件）
  report.json  完整原始数据，任何结论都能自行复核

退出码
------
  0  未发现达到 --fail-on 的问题
  1  发现达到 --fail-on 的问题（默认 high）
  2  运行失败（无法连接、密钥无效等）
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="relaycheck",
        description="检测 LLM API 中转站是否掉包模型、是否虚报计费。",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-u", "--base-url", help="中转站地址，如 https://api.example.com（可带 /v1）")
    p.add_argument("-k", "--api-key", help="API Key（也可用环境变量 RELAYCHECK_API_KEY）")
    p.add_argument(
        "-m", "--models",
        help="要审计的模型名，逗号分隔。缺省时自动从 /v1/models 里挑（按厂商多样性优先）",
    )
    p.add_argument(
        "--max-models", type=int, default=6,
        help="最多审计多少个模型（默认 6，控制请求数与花费）",
    )
    p.add_argument(
        "--probes",
        help=f"要跑的探针，逗号分隔，或 all。可选：{', '.join(probe_names())}",
    )
    p.add_argument("--out-dir", help="输出目录（默认 relaycheck-<host>-<时间戳>）")
    p.add_argument("--timeout", type=float, default=60.0, help="单次请求超时秒数（默认 60）")
    p.add_argument(
        "--delay", type=float, default=0.4,
        help="两次请求之间的最小间隔秒数（默认 0.4，避免触发限流）",
    )
    p.add_argument("--max-retries", type=int, default=3, help="失败重试次数（默认 3）")
    p.add_argument(
        "--budget", type=float, default=240.0, metavar="SECONDS",
        help=(
            "每个探针的墙钟预算秒数（默认 240）。中转站很慢时探针会在预算用尽后"
            "提前结束，并在报告里如实标注为「不完整」而不是「通过」。"
            "设为 0 表示不限制。"
        ),
    )
    p.add_argument(
        "--reliability-samples", type=int, default=8, metavar="N",
        help="可用性探针的采样次数（默认 8）",
    )
    p.add_argument(
        "--context-sizes", metavar="A,B,C",
        help=(
            "上下文探针的测试深度（token 数，逗号分隔，默认 2000,8000,32000）。"
            "探针从最浅一档开始逐级加深，一旦发现截断就停止。"
        ),
    )
    p.add_argument(
        "--context-max-models", type=int, default=1, metavar="N",
        help="上下文探针最多测几个模型（默认 1；该探针每档深度都要发一次长请求，很贵）",
    )
    p.add_argument("--user-agent", default=DEFAULT_BROWSER_UA, help="自定义 User-Agent")
    p.add_argument(
        "--header", action="append", default=[], metavar="K:V",
        help="附加请求头，可重复，如 --header 'X-Token: abc'",
    )
    p.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验（不推荐）")
    p.add_argument(
        "--fail-on", choices=["none", "medium", "high", "critical", "low"], default="high",
        help="达到该级别即返回退出码 1（默认 high）",
    )
    p.add_argument("--list-probes", action="store_true", help="列出所有探针后退出")
    p.add_argument("--list-models", action="store_true", help="只列出可用模型后退出")
    p.add_argument("-v", "--verbose", action="store_true", help="打印每个请求的细节")
    p.add_argument("--version", action="version", version=f"relaycheck {__version__}")
    return p


def _make_output_safe() -> None:
    """Stop a printing problem from being able to kill an audit.

    Windows consoles usually run a legacy code page (cp936, cp1252, ...). A
    progress line containing a glyph that code page lacks used to raise
    ``UnicodeEncodeError`` **inside the probe loop**, aborting the whole run
    before any report was written. Worse, the traceback's exit status is 1 —
    the very code this tool reserves for "findings at or above --fail-on", so a
    cosmetic bug became indistinguishable from a verdict.

    Only ``errors`` is relaxed, never ``encoding``: keeping the console's own
    code page means Chinese output stays readable in cmd.exe, and anything the
    code page genuinely cannot represent degrades to ``?`` instead of taking
    the audit down with it.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):  # pragma: no cover - exotic streams
            pass


def force_utf8_output() -> None:
    """Force UTF-8 on stdout/stderr, for callers whose reader expects UTF-8.

    Two callers need this and neither is the plain pip-installed CLI:

    * the bundled desktop engine, because a **frozen PyInstaller app ignores
      ``PYTHONIOENCODING``** — measured on the 6.22.3 Windows build, the parent set
      it to ``utf-8`` and the frozen child still emitted GBK (``目标`` as
      ``\\xc4\\xbf\\xb1\\xea``). The GUI decodes that pipe as UTF-8, so without this the
      log window is mojibake while ``report.json`` is perfectly correct;
    * the GUI re-dispatching into itself, where the stream may also have started out
      as ``None``.

    On a real Windows console this changes nothing: Python 3.6+ drives the console
    through ``WriteConsoleW`` and already reports ``utf-8``. It only affects a pipe
    or a redirected file — which is exactly the case that was broken.

    ``errors="replace"`` is kept so an unrepresentable character degrades to ``?``
    rather than aborting the audit.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):  # pragma: no cover - exotic streams
            pass


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Every failure mode must land on a distinct exit code."""
    _make_output_safe()
    try:
        return _run(argv)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - the exit code is the whole point
        traceback.print_exc()
        print(
            f"错误：审计未能完成（{type(exc).__name__}: {exc}）。\n"
            "      没有生成报告 —— 这不代表发现了问题，也不代表中转站没问题，"
            "只代表这次没查成。",
            file=sys.stderr,
        )
        return EXIT_ERROR


def _run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_probes:
        for probe in select_probes(["all"]):
            print(f"{probe.name:<28} {probe.description}")
        return EXIT_OK

    base_url = args.base_url or os.environ.get("RELAYCHECK_BASE_URL") or os.environ.get(
        "OPENAI_BASE_URL"
    )
    api_key = args.api_key or os.environ.get("RELAYCHECK_API_KEY") or os.environ.get(
        "OPENAI_API_KEY"
    )
    if not base_url:
        print("错误：缺少 --base-url（或设置 RELAYCHECK_BASE_URL）", file=sys.stderr)
        return EXIT_ERROR
    if not api_key:
        print("错误：缺少 --api-key（或设置 RELAYCHECK_API_KEY）", file=sys.stderr)
        return EXIT_ERROR

    try:
        probes = select_probes(_split(args.probes))
    except KeyError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_ERROR

    extra_headers = _parse_headers(args.header)
    client = RelayClient(
        base_url,
        api_key,
        timeout=args.timeout,
        max_retries=args.max_retries,
        user_agent=args.user_agent,
        extra_headers=extra_headers,
        delay_between_requests=args.delay,
        verbose=args.verbose,
    )
    if args.insecure:
        client.session.verify = False
        import urllib3

        urllib3.disable_warnings()  # noqa: S101 - narrowly scoped, user opted in

    print(f"relaycheck {__version__}")
    print(f"目标: {client.base_url}")

    # ---------------------------------------------------------------- models
    available: list[str] = []
    notes: list[str] = []
    try:
        available = client.list_models()
    except RelayError as exc:
        notes.append(f"无法获取模型列表（{exc}）；将只使用 --models 指定的模型")
        print(f"警告: 无法获取 /v1/models（{exc}）")

    if args.list_models:
        for name in available:
            print(name)
        if not available:
            print("(空)")
        return EXIT_OK

    explicit = _split(args.models)
    if explicit:
        unknown = [m for m in explicit if available and m not in available]
        if unknown:
            notes.append(f"以下模型不在 /v1/models 返回中，仍会尝试：{', '.join(unknown)}")
        models = explicit[: max(1, args.max_models)]
        selection_mode = "explicit"
    else:
        if not available:
            print("错误：没有可用模型，且未指定 --models", file=sys.stderr)
            return EXIT_ERROR
        models = select_models(available, max(1, args.max_models))
        selection_mode = "auto"

    if not models:
        print("错误：没有可审计的模型", file=sys.stderr)
        return EXIT_ERROR

    print(f"可用模型 {len(available)} 个，受测 {len(models)} 个: {describe_selection(models)}")
    print(f"探针: {', '.join(p.name for p in probes)}")
    if args.max_models < len([m for m in available if m]) and not explicit:
        notes.append(
            f"仅审计了 {len(models)}/{len(available)} 个模型（--max-models {args.max_models}）"
        )
    print("")

    # ------------------------------------------------------------------ run
    probe_options: dict[str, Any] = {
        "probe_budget_s": (args.budget if args.budget and args.budget > 0 else float("inf")),
        "reliability_samples": max(1, args.reliability_samples),
        "context_sizes": args.context_sizes,
        "context_max_models": max(1, args.context_max_models),
    }
    ctx = ProbeContext(
        client=client,
        models=models,
        max_calls=max(40, 40 * max(1, len(models))),
        options=probe_options,
    )

    started = utc_now_iso()
    t0 = time.monotonic()

    def _on_probe(probe: Any) -> None:
        print(f"  → {probe.name} …", flush=True)

    def _on_progress(message: str) -> None:
        # Intra-probe heartbeat. A probe that loops over models on a relay that
        # times out can run for minutes; without a heartbeat the audit looks
        # hung, and the operator cannot tell "slow" from "stuck".
        print(f"      · {message}", flush=True)

    def _on_result(probe: Any, res: Any) -> None:
        worst = res.findings[0].severity.value if res.findings else "clean"
        flag = "错误" if res.error else worst
        extra = ""
        if res.data.get("truncated"):
            extra = " [预算用尽，未跑完]"
        print(
            f"    √ {probe.name}: {flag} "
            f"({res.requests_made} 请求 / {res.duration_s:.1f}s){extra}",
            flush=True,
        )
        if probe.name == "reliability":
            stats = res.data.get("reliability") or {}
            if stats:
                p50 = stats.get("p50_latency_s")
                # ``None`` means nothing succeeded, so there is no latency to
                # report. "n/as" would be the alternative.
                latency = f"中位延迟 {p50:.1f}s" if p50 is not None else "无成功样本，无法给出延迟"
                print(
                    f"      成功率 {stats.get('succeeded')}/{stats.get('samples')}"
                    f"（失败率 {stats.get('failure_rate', 0):.0%}），{latency}",
                    flush=True,
                )
                if stats.get("failure_rate", 0) > 0.2:
                    notes.append(
                        "中转站可用性很差（失败率 "
                        f"{stats['failure_rate']:.0%}），"
                        "其余探针的结论可能不完整；报告中已逐项标注。"
                    )

    try:
        results = run_probes(
            probes, ctx, on_probe=_on_probe, on_result=_on_result, on_progress=_on_progress
        )
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return EXIT_ERROR

    duration = time.monotonic() - t0

    report = Report(
        target=client.base_url,
        models=models,
        available_models=available,
        selection_mode=selection_mode,
        selection_rationale=describe_selection(models),
        probe_names=[p.name for p in probes],
        results=results,
        tool_version=__version__,
        started_at=started,
        duration_s=duration,
        requests_made=client.request_count,
        notes=notes,
    )

    # --------------------------------------------------------------- output
    out_dir = Path(args.out_dir) if args.out_dir else Path(_default_out_dir(client.base_url))
    json_path = report.write_json(out_dir / "report.json")
    md_path = report.write_markdown(out_dir / "report.md")

    print("")
    print(render_text(report))
    print(f"报告: {md_path}")
    print(f"原始: {json_path}")

    return _exit_code(report, args.fail_on)


# --------------------------------------------------------------------- helpers


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in re.split(r"[,\n]", value) if item.strip()]


def _parse_headers(items: Sequence[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in items:
        if ":" not in item:
            continue
        key, _, value = item.partition(":")
        headers[key.strip()] = value.strip()
    return headers


def _default_out_dir(base_url: str) -> str:
    host = urlparse(base_url).netloc or "relay"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", host)
    return f"relaycheck-{safe}-{time.strftime('%Y%m%d-%H%M%S')}"


def _exit_code(report: Report, fail_on: str) -> int:
    if fail_on == "none":
        return EXIT_OK
    threshold = {
        "critical": Severity.CRITICAL,
        "high": Severity.HIGH,
        "medium": Severity.MEDIUM,
        "low": Severity.LOW,
    }[fail_on]
    order = list(SEVERITY_ORDER)
    limit = order.index(threshold)
    for finding in report.all_findings:
        if order.index(finding.severity) <= limit and finding.severity is not Severity.CLEAN:
            return EXIT_FINDINGS
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
