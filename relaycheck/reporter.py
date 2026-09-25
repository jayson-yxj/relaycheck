"""Reporting: console, JSON and Markdown.

Design rules:

* Findings are always ordered worst-first, and every one carries its confidence
  separately from its severity. "This is bad" and "we are sure it is bad" are
  different claims and the reader must be able to tell them apart.
* Raw probe data is always exported to JSON, so a reader can re-derive any
  conclusion instead of trusting the summary.
* Nothing is claimed that the evidence does not support. When a probe could not
  run, that is stated as a limitation rather than silently dropped.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import SEVERITY_ORDER, Confidence, Finding, ProbeResult, Severity


def _write_text_lf(path: Path, text: str) -> None:
    """Write UTF-8 with LF endings on every platform.

    ``Path.write_text`` translates ``\\n`` to ``os.linesep``, so the same audit
    produced a CRLF report on Windows and an LF report on Linux. That defeats
    the point of the JSON export: two runs of the same tool against the same
    relay should differ only in the fields that genuinely differ, and a
    whole-file line-ending diff hides which ones those are. Passed explicitly
    rather than via ``write_text(newline=...)``, which needs Python 3.10.
    """
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


_SEVERITY_TAG = {
    Severity.CRITICAL: "CRITICAL",
    Severity.HIGH: "HIGH",
    Severity.MEDIUM: "MEDIUM",
    Severity.LOW: "LOW",
    Severity.INFO: "INFO",
    Severity.CLEAN: "CLEAN",
}

_CONFIDENCE_TAG = {
    Confidence.CONFIRMED: "已确认",
    Confidence.LIKELY: "很可能",
    Confidence.SUSPECTED: "疑似",
}


@dataclass
class Report:
    target: str
    models: list[str] = field(default_factory=list)
    available_models: list[str] = field(default_factory=list)
    probe_names: list[str] = field(default_factory=list)
    results: list[ProbeResult] = field(default_factory=list)
    tool_version: str = ""
    started_at: str = ""
    duration_s: float = 0.0
    requests_made: int = 0
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ derived

    @property
    def all_findings(self) -> list[Finding]:
        out: list[Finding] = []
        for r in self.results:
            out.extend(r.findings)
        return sorted(out, key=lambda f: f.sort_key())

    @property
    def counts(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Severity}
        for f in self.all_findings:
            counts[f.severity.value] += 1
        return counts

    @property
    def verdict(self) -> str:
        counts = self.counts
        if counts[Severity.CRITICAL.value]:
            return "检测到可直接定性的掉包证据"
        if counts[Severity.HIGH.value]:
            return "检测到高风险问题"
        if counts[Severity.MEDIUM.value]:
            return "检测到中等问题"
        if counts[Severity.LOW.value]:
            return "仅检测到轻微问题"
        return "未检测到问题"

    def worst_severity(self) -> Severity:
        findings = self.all_findings
        return findings[0].severity if findings else Severity.CLEAN

    # ------------------------------------------------------------------ exports

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": "relaycheck",
            "tool_version": self.tool_version,
            "target": self.target,
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 3),
            "requests_made": self.requests_made,
            "models_tested": self.models,
            "models_available_count": len(self.available_models),
            "probes": self.probe_names,
            "verdict": self.verdict,
            "severity_counts": self.counts,
            "notes": self.notes,
            "findings": [f.to_dict() for f in self.all_findings],
            "results": [r.to_dict() for r in self.results],
        }

    def write_json(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_text_lf(p, json.dumps(self.to_dict(), ensure_ascii=False, indent=2))
        return p

    def write_markdown(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_text_lf(p, render_markdown(self))
        return p


def render_text(report: Report) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("relaycheck — LLM 中转站掉包与计费审计")
    lines.append("=" * 72)
    lines.append(f"目标        : {report.target}")
    lines.append(f"开始时间    : {report.started_at}")
    lines.append(f"耗时        : {report.duration_s:.1f}s（{report.requests_made} 次请求）")
    lines.append(f"探针        : {', '.join(report.probe_names)}")
    lines.append(f"可用模型数  : {len(report.available_models)}")
    lines.append(f"受测模型    : {', '.join(report.models) or '(无)'}")
    for note in report.notes:
        lines.append(f"备注        : {note}")
    lines.append("")

    counts = report.counts
    lines.append("结论        : " + report.verdict)
    lines.append(
        "问题数量    : "
        + "  ".join(
            f"{_SEVERITY_TAG[s]}={counts[s.value]}"
            for s in SEVERITY_ORDER
            if s is not Severity.CLEAN
        )
    )
    lines.append("")

    findings = [f for f in report.all_findings if f.severity is not Severity.CLEAN]
    if findings:
        lines.append("-" * 72)
        lines.append("发现（按严重程度排序）")
        lines.append("-" * 72)
        for f in findings:
            lines.append("")
            lines.append(f"[{_SEVERITY_TAG[f.severity]}] {f.id}  {f.title}")
            lines.append(f"  可信度: {_CONFIDENCE_TAG[f.confidence]}")
            for para in _wrap(f.summary, 68):
                lines.append(f"  {para}")
            if f.remediation:
                lines.append(f"  建议: {f.remediation}")
            ev = _compact_evidence(f.evidence)
            if ev:
                lines.append(f"  证据: {ev}")
    else:
        lines.append("未发现非 CLEAN 级别的问题。")

    clean = [f for f in report.all_findings if f.severity is Severity.CLEAN]
    if clean:
        lines.append("")
        lines.append("-" * 72)
        lines.append("已核实正常（CLEAN）")
        lines.append("-" * 72)
        for f in clean:
            lines.append(f"  [OK] {f.probe if hasattr(f, 'probe') else ''}{f.id}  {f.title}")

    incomplete = [r for r in report.results if r.error or r.data.get("truncated")]
    if incomplete:
        lines.append("")
        lines.append("-" * 72)
        lines.append("未能跑完的探针（属审计局限，不是 CLEAN）")
        lines.append("-" * 72)
        for r in incomplete:
            if r.error:
                lines.append(f"  [!] {r.probe}: 探针崩溃 — {r.error}")
            else:
                detail = r.data.get("truncated_detail") or "探针时间预算用尽"
                lines.append(
                    f"  [~] {r.probe}: 提前结束（{r.requests_made} 请求 / "
                    f"{r.duration_s:.0f}s）— {detail}"
                )

    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    md: list[str] = []
    md.append("# relaycheck 审计报告")
    md.append("")
    md.append("| 项目 | 值 |")
    md.append("| --- | --- |")
    md.append(f"| 目标 | `{report.target}` |")
    md.append(f"| 时间 | {report.started_at} |")
    md.append(f"| 耗时 | {report.duration_s:.1f}s / {report.requests_made} 次请求 |")
    md.append(f"| 工具版本 | {report.tool_version} |")
    md.append(f"| 探针 | {', '.join(report.probe_names)} |")
    md.append(f"| 可用模型数 | {len(report.available_models)} |")
    md.append(f"| 受测模型 | {', '.join(f'`{m}`' for m in report.models) or '(无)'} |")
    md.append("")
    for note in report.notes:
        md.append(f"> 备注：{note}")
    if report.notes:
        md.append("")

    counts = report.counts
    md.append("## 结论")
    md.append("")
    md.append(f"**{report.verdict}**")
    md.append("")
    md.append("| 严重程度 | 数量 |")
    md.append("| --- | --- |")
    for sev in SEVERITY_ORDER:
        md.append(f"| {_SEVERITY_TAG[sev]} | {counts[sev.value]} |")
    md.append("")

    findings = [f for f in report.all_findings if f.severity is not Severity.CLEAN]
    md.append("## 发现")
    md.append("")
    if not findings:
        md.append("未发现非 CLEAN 级别的问题。")
    for f in findings:
        md.append(f"### [{_SEVERITY_TAG[f.severity]}] {f.title}")
        md.append("")
        md.append(f"- **编号**：`{f.id}`")
        md.append(f"- **可信度**：{_CONFIDENCE_TAG[f.confidence]}")
        md.append("")
        md.append(f.summary)
        md.append("")
        if f.remediation:
            md.append(f"> **建议**：{f.remediation}")
            md.append("")
        if f.evidence:
            md.append("<details><summary>证据</summary>")
            md.append("")
            md.append("```json")
            md.append(json.dumps(f.evidence, ensure_ascii=False, indent=2)[:8000])
            md.append("```")
            md.append("")
            md.append("</details>")
            md.append("")

    clean = [f for f in report.all_findings if f.severity is Severity.CLEAN]
    if clean:
        md.append("## 已核实正常")
        md.append("")
        for f in clean:
            md.append(f"- `{f.id}` {f.title}")
        md.append("")

    incomplete = [r for r in report.results if r.error or r.data.get("truncated")]
    if incomplete:
        md.append("## 审计局限")
        md.append("")
        md.append("以下探针**没有跑完**。它们给出的「没发现问题」不构成结论，")
        md.append("只能说这一项未经检验。")
        md.append("")
        for r in incomplete:
            if r.error:
                md.append(f"- 探针 `{r.probe}` 崩溃，未完成：{r.error}")
            else:
                detail = r.data.get("truncated_detail") or "探针时间预算用尽"
                md.append(
                    f"- 探针 `{r.probe}` 提前结束（{r.requests_made} 请求 / "
                    f"{r.duration_s:.0f}s）：{detail}"
                )
        md.append("")

    md.append("## 原始数据")
    md.append("")
    md.append("完整原始返回见同目录 JSON 报告，可自行复核任何结论。")
    md.append("")
    return "\n".join(md)


# --------------------------------------------------------------------- helpers


def _wrap(text: str, width: int) -> list[str]:
    out: list[str] = []
    for raw_line in text.splitlines() or [""]:
        line = raw_line.strip()
        while len(line) > width:
            out.append(line[:width])
            line = line[width:]
        out.append(line)
    return out


def _compact_evidence(evidence: dict[str, Any], limit: int = 220) -> str:
    if not evidence:
        return ""
    try:
        text = json.dumps(evidence, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(evidence)
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} 字符，见 JSON)"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
