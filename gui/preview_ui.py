"""Local, zero-request preview states for relaycheck's desktop shell.

Usage::

    python gui/preview_ui.py clean
    python gui/preview_ui.py problem

This is a design harness, not a second reporting implementation.  It only feeds
representative, static data into ``RelayCheckApp``'s existing rendering methods;
it never creates a client, starts an audit, reads a key, or writes a report.
"""

from __future__ import annotations

import argparse
import tkinter as tk
from pathlib import Path

import relaycheck_gui as G


def _identity_report(contradiction: bool) -> dict:
    reported = "openai" if contradiction else "deepseek"
    verdict = "contradiction" if contradiction else "consistent"
    findings = []
    if contradiction:
        findings.append(
            {
                "id": "id-100",
                "evidence": {
                    "contradictions": [
                        {
                            "model": "deepseek-v4-flash",
                            "expected_family": "deepseek",
                            "reported_family": [reported],
                            "quote": "I was created by OpenAI.",
                        }
                    ]
                },
            }
        )
    return {
        "findings": findings,
        "results": [
            {
                "probe": "identity",
                "data": {
                    "observations": {
                        "deepseek-v4-flash": {
                            "expected_family": "deepseek",
                            "self_reported_families": [reported],
                            "confirmed_families": [reported] if contradiction else [],
                            "verdict": verdict,
                        },
                        "deepseek-flash": {
                            "expected_family": "deepseek",
                            "self_reported_families": ["deepseek"],
                            "confirmed_families": [],
                            "verdict": "consistent",
                        },
                    }
                },
            }
        ],
    }


def _fill_log(app: G.RelayCheckApp) -> None:
    for line in (
        "目标: https://api.example.com/v1",
        "探针: reliability, echo, tokenizer, twins, identity, canary, billing, params",
        "  √ reliability: clean (8 请求 / 4.2s)",
        "  √ echo: info (4 请求 / 3.8s)",
        "  √ tokenizer: clean (18 请求 / 12.5s)",
    ):
        app._log(line)


def show(state: str) -> None:
    root = tk.Tk()
    app = G.RelayCheckApp(root)
    app.url_var.set("https://api.example.com/v1")
    app.outdir_var.set(str(Path.home() / "Documents" / "relaycheck报告" / "preview"))

    if state == "initial":
        pass
    elif state == "advanced":
        app._toggle_advanced()
    elif state == "running":
        app._set_running(True)
        app._set_progress(5, 8)
        app.status_var.set("第 6/8 项：identity")
        app.status_dot.configure(foreground=G._ACCENT)
        app._show_card("正在检测…", "已启动，输出会实时显示在下面。", None)
        _fill_log(app)
    elif state == "clean":
        app._set_progress(8, 8)
        app.status_var.set("结束（退出码 0）")
        report = _identity_report(False)
        app._show_card(
            "未检测到问题",
            G.VERDICT_STYLE["未检测到问题"][2],
            {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 7, "clean": 2},
            G._family_rows(report),
            actions=True,
        )
        _fill_log(app)
    elif state == "problem":
        app._set_progress(8, 8)
        app.status_var.set("结束（退出码 1）")
        report = _identity_report(True)
        app._show_card(
            "检测到高风险问题",
            G.VERDICT_STYLE["检测到高风险问题"][2]
            + "\n最严重的一条：twins-100  不同名称返回了逐字一致的开放式回答",
            {"critical": 0, "high": 2, "medium": 1, "low": 0, "info": 4, "clean": 1},
            G._family_rows(report),
            actions=True,
        )
        _fill_log(app)
    elif state == "failure":
        fg, bg, detail = G._FAIL_STYLE
        app.status_var.set("提前结束（3/8 项，退出码 2）")
        app._set_progress(3, 8)
        app._show_card("运行失败", detail, None, actions=True)
        app._tint(bg)
        app.card_accent.configure(background=fg)
        app.verdict_label.configure(foreground=fg)
        app._log("连接失败：示例错误；没有生成报告。")
    else:  # argparse constrains this; kept loud for direct callers.
        raise ValueError(state)

    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description="预览 relaycheck 桌面版的静态界面状态")
    parser.add_argument(
        "state",
        nargs="?",
        default="clean",
        choices=("initial", "advanced", "running", "clean", "problem", "failure"),
    )
    args = parser.parse_args()
    show(args.state)


if __name__ == "__main__":
    main()
