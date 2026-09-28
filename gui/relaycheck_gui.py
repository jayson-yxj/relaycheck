"""relaycheck 桌面版 —— 给不装 Python、也不用命令行的人一个能双击跑的外壳。

Why this file exists
--------------------
命令行才是真产品，这里只是一层壳。它把界面上的输入拼成**和命令行完全一样的
argv**，交给 ``relaycheck.cli`` 去跑，再把输出显示出来。审计逻辑一行都不在这
里重写 —— 所以界面永远不可能给出和命令行不一样的结论，而钉住命令行的那套验收
测试也就等于钉住了整个工具。

Two deliberate choices
----------------------
1. **API Key 走环境变量，绝不进命令行。** Windows 上任何进程都能通过 WMI 读到
   别的进程的命令行，把 key 放那儿等于全机器可见。环境变量块不是全局可读的。

2. **审计跑在子进程里，不是线程。** 线程停不干净：一个线程正卡在 60 秒的 socket
   超时里，tkinter 只能干等，所谓「停止」按钮就是假的。子进程可以直接终止，这才
   是「停止」按钮真正做的事。

Report honesty
--------------
界面上的结论卡片刻意复述命令行报告里的措辞。「未检测到问题」下面一定会跟一句
「这只代表这次没查出来，不等于这家站一定没问题」—— 这是本工具的核心约束，
不能因为套了个图形界面就把话说满。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


# --------------------------------------------------------------------- bootstrap

def _repo_root() -> Path:
    """Where the ``relaycheck`` package lives, both frozen and from source."""
    return Path(__file__).resolve().parent.parent


def _ensure_importable() -> None:
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)


_ensure_importable()

from relaycheck import __version__  # noqa: E402
from relaycheck.client import RelayClient, RelayError  # noqa: E402

APP_TITLE = "relaycheck 桌面版"

# ---------------------------------------------------------------- palettes

# 窗口里每个颜色都按「角色」取名（画布 / 禁用按钮底 / CLEAN 小胶囊），不按颜色值
# 取名 —— 同一个角色在深色画布上必须是另一个值。于是一个主题就是一张「角色 ->
# 色值」表，而切主题只有两步：换掉 ``PALETTE``，重建控件树，没有第三种机制。
#
# 表里有两条必须守住的规则：
#
# * severity 小胶囊是压在页面上的白字色块，不是画在页面上的元素，所以它在两个主题
#   里一个字都不改 —— 这是颜色和背景唯一互不影响的地方。
# * 绿是 CLEAN 的语义色，任何主题里都不能拿它当品牌色或主按钮色。

#: 每个主题都必须提供的角色。少了哪一个，测试会立刻说出来。
_CHROME_ROLES = (
    "app_bg", "surface", "surface_subtle", "text", "text_muted", "text_faint",
    "border", "border_soft", "accent", "accent_hover", "accent_soft", "on_accent",
    "danger", "danger_soft", "button_secondary_bg", "button_secondary_hover",
    "button_danger_hover", "button_disabled_bg", "track", "rule",
    #: 进度条的填充。它是唯一一个「只表示在动」的填充色，所以既不能借用任何语义
    #: 色，也不能跟着强调色走 —— 强调色在本项目里是红，而一条红进度条会被读成
    #: 「出错了」。两个主题都给它一个中性到发亮的值。
    "progress_fill",
    "shade_fallback", "spend_warn", "log_bg", "log_border", "log_head_bg",
    "log_head_fg", "log_hint_fg", "log_text_bg", "log_text_fg", "log_caret",
    "log_select_bg", "log_select_fg", "sash_hint", "severity_empty_fg",
)

#: 卡片上那句话的措辞。跟颜色无关，所以不跟着主题走。
_VERDICT_TEXT: dict[str, str] = {
    "检测到可直接定性的掉包证据": (
        "被测站返回了无法用正常行为解释的证据。可以把这个目录里的 report.md "
        "直接发给商家对质。"
    ),
    "检测到高风险问题": "发现了值得认真对待的问题。请打开 report.md 看具体是哪几条。",
    "检测到中等问题": "发现了异常，但单独一条不足以定性。建议结合报告自行复核。",
    "仅检测到轻微问题": "只有轻微异常。多数情况是正常转售带来的副作用，不构成指控。",
    "未检测到问题": (
        "本次没有发现达到阈值的问题。注意：这只代表「这次没查出来」，"
        "不等于「这家站一定没问题」—— 本工具只能给到家族级线索。"
    ),
}

_FAIL_TEXT = (
    "这次没查成，没有生成报告。这不代表中转站有问题，也不代表没问题 —— "
    "只代表这一次没查成。下面是原始输出。"
)

_SEVERITY_CHIPS: tuple[tuple[str, str], ...] = (
    ("critical", "CRITICAL"),
    ("high", "HIGH"),
    ("medium", "MEDIUM"),
    ("low", "LOW"),
    ("info", "INFO"),
    ("clean", "CLEAN"),
)

#: The 高级 disclosure, closed and open. The panel holds three settings that are
#: all already correct for the overwhelming majority of runs, and it costs about a
#: third of the form's height to show them. Closed by default is the point — the
#: title says 通常无需调整, so a window that shows it anyway is arguing with itself.
_ADV_CLOSED = "›  高级选项（通常无需调整）"
_ADV_OPEN = "⌄  高级选项（通常无需调整）"

LIGHT: dict[str, Any] = {
    # 画布与分层
    "app_bg": "#f4f6fb",
    "surface": "#ffffff",
    "surface_subtle": "#f8fafc",
    "border": "#d0d5dd",
    "border_soft": "#e4e7ec",
    "rule": "#dddddd",
    "track": "#eaecf0",
    "sash_hint": "#c7cdd8",
    "shade_fallback": "#dddddd",
    #: 进度条填充。装完这套角色之后它拿的就是原来 ``accent`` 的靛蓝值，一个字节
    #: 都没变 —— 第五刀把浅色 accent 换成品牌红的时候，进度条不会跟着红。
    "progress_fill": "#4f46e5",
    # 文字。text_faint 在白底上只有 2.58:1 —— 它只用在「这一栏这次没测」这种
    # 明确次要的说明上，不承载任何结论。
    "text": "#101828",
    "text_muted": "#667085",
    "text_faint": "#98a2b3",
    # 品牌与语义。绿留给 CLEAN，绝不在这里出现。
    "accent": "#4f46e5",
    "accent_hover": "#4338ca",
    "accent_soft": "#eef2ff",
    "on_accent": "#ffffff",
    "danger": "#b42318",
    "danger_soft": "#fef3f2",
    "spend_warn": "#a1541a",
    "severity_empty_fg": "#78716c",
    # 按钮
    "button_secondary_bg": "#f2f4f7",
    "button_secondary_hover": "#e4e7ec",
    "button_danger_hover": "#fee4e2",
    "button_disabled_bg": "#eaecf0",
    # 日志面板。浅色下它是压在浅色页面上的黑板，是下半屏的视觉锚点。
    "log_bg": "#101828",
    "log_border": "#344054",
    "log_head_bg": "#101828",
    "log_head_fg": "#f2f4f7",
    "log_hint_fg": "#667085",
    "log_text_bg": "#0b1220",
    "log_text_fg": "#d0d5dd",
    "log_caret": "#ffffff",
    "log_select_bg": "#344054",
    "log_select_fg": "#ffffff",
    # 结论卡片：``(前景, 背景)``，与 ``_VERDICT_TEXT`` 同一批 key。
    "verdict": {
        "检测到可直接定性的掉包证据": ("#7f1d1d", "#fee2e2"),
        "检测到高风险问题": ("#7f1d1d", "#fee2e2"),
        "检测到中等问题": ("#78350f", "#fef3c7"),
        "仅检测到轻微问题": ("#713f12", "#fef9c3"),
        "未检测到问题": ("#14532d", "#dcfce7"),
    },
    #: 认不出来的 verdict 走中性卡片，而不是让窗口在一次三分钟的检测之后崩掉。
    "verdict_fallback": ("#344054", "#f2f4f7"),
    "fail": ("#7f1d1d", "#fee2e2"),
    #: 白字填充块，两个主题共用（见文件头第 1 条规则）。一个读数是零的胶囊故意
    #: 不填色：``CRITICAL=0`` 配一块实心红是警报，而干净的一跑里它正好说反了。
    "severity_fill": {
        "critical": ("#b91c1c", "#ffffff"),
        "high": ("#c2410c", "#ffffff"),
        "medium": ("#a16207", "#ffffff"),
        "low": ("#4d7c0f", "#ffffff"),
        "info": ("#475569", "#ffffff"),
        "clean": ("#15803d", "#ffffff"),
    },
    # 厂商色。给**自称的**厂商上色才是重点：两个都答「I was created by OpenAI」
    # 的模型亮同一个色，那条模板化回答才看得出来。这是身份分组，不是定罪 ——
    # 一个色说明「这两句话是同一句话」，从不说「这个模型是假的」。
    "family": {
        "openai": "#0f766e",
        "anthropic": "#7c3aed",
        "deepseek": "#1d4ed8",
        "google": "#b45309",
        "meta": "#0369a1",
        "mistral": "#c2410c",
        "minimax": "#be123c",
        "qwen": "#4d7c0f",
        "zhipu": "#a21caf",
        "moonshot": "#0e7490",
        "xai": "#374151",
        "cohere": "#9333ea",
        "amazon": "#a16207",
        "microsoft": "#1e40af",
        "nvidia": "#15803d",
    },
    #: 没见过的厂商也要拿到一个稳定的色，而不是没有色。用确定性哈希选槽，绝不用
    #: ``hash()`` —— 那个每进程加盐，同一份报告每次跑出来颜色都不一样。
    "family_fallback": ("#0f766e", "#1d4ed8", "#be123c", "#a16207", "#7c3aed", "#0e7490"),
}

DARK: dict[str, Any] = {
    # 画布与分层。画布本身还叠着一层极缓的暗深红渐变（#2a1418 -> #190f12 ->
    # #0d0809），这里的 app_bg 是它的中段，也是渐变失效时的单色兜底 —— 所以
    # 即使渐变没画上，暗色主题也还是完整的。
    "app_bg": "#190f12",
    "surface": "#1e1619",
    "surface_subtle": "#261c1f",
    "border": "#463538",
    "border_soft": "#332729",
    "rule": "#332729",
    "track": "#332729",
    "sash_hint": "#463538",
    #: 只在 `_shade()` 拿到一个解析不了的颜色时才用得上，正常路径走不到。
    "shade_fallback": "#332729",
    #: 进度条填充。深色下它不跟着 accent 变红，而是一个偏暖的亮中性色：对进度槽
    #: ``#332729`` 是 5.61:1，跟浅色那根靛蓝条对它的槽 5.90:1 基本对齐。
    "progress_fill": "#ab9fa1",
    # 文字。深色下 text_faint 反而达标了 —— 浅色那个 #98a2b3 在白底上只有
    # 2.58:1，本来就不合格。
    "text": "#f4eeee",
    "text_muted": "#ab9fa1",
    "text_faint": "#7d7274",
    # 品牌与语义。深色里红是强调色，它压在画布上是 4.94:1。
    "accent": "#e5484d",
    "accent_hover": "#f26064",
    "accent_soft": "#241014",
    "on_accent": "#1a0d0e",
    "danger": "#ff9a94",
    "danger_soft": "#3f1c1f",
    "spend_warn": "#e8a33d",
    "severity_empty_fg": "#8a7f80",
    # 按钮
    "button_secondary_bg": "#2a2023",
    "button_secondary_hover": "#352a2d",
    "button_danger_hover": "#4a2226",
    "button_disabled_bg": "#2b2326",
    # 日志面板。浅色下它是压在浅色页面上的黑板，是「往里看的窗口」；深色下没有
    # 任何东西比黑画布更黑，于是把它做成凹陷：底色比画布还深一点，靠 1.78:1 的
    # 边框把它从画布上划出来。
    "log_bg": "#080506",
    "log_border": "#463538",
    "log_head_bg": "#100b0c",
    "log_head_fg": "#e6dede",
    "log_hint_fg": "#8d8285",
    "log_text_bg": "#080506",
    "log_text_fg": "#c9c0c0",
    "log_caret": "#e5484d",
    "log_select_bg": "#3f3134",
    "log_select_fg": "#ffffff",
    "verdict": {
        "检测到可直接定性的掉包证据": ("#ffb3ad", "#4a1b20"),
        "检测到高风险问题": ("#ffb3ad", "#4a1b20"),
        "检测到中等问题": ("#ffd6a0", "#432d12"),
        "仅检测到轻微问题": ("#efe291", "#3d351a"),
        "未检测到问题": ("#8fe4b1", "#153424"),
    },
    "verdict_fallback": ("#d0c7c8", "#2a2023"),
    "fail": ("#ffb3ad", "#4a1b20"),
    #: **和浅色一个字都不差。** 见文件头第 1 条规则：这是压在页面上的白字色块，
    #: 不是画在页面上的元素。
    "severity_fill": {
        "critical": ("#b91c1c", "#ffffff"),
        "high": ("#c2410c", "#ffffff"),
        "medium": ("#a16207", "#ffffff"),
        "low": ("#4d7c0f", "#ffffff"),
        "info": ("#475569", "#ffffff"),
        "clean": ("#15803d", "#ffffff"),
    },
    # 厂商色（15 个）全部重取。浅色那套是给白底挑的，搬到深色画布上会发糊 ——
    # 最亮的几个直接变成灰。重取之后最差的 cohere 也有 6.52:1。
    "family": {
        "openai": "#2dd4bf",
        "anthropic": "#c084fc",
        "deepseek": "#7aa2ff",
        "google": "#f0b429",
        "meta": "#4cc3f0",
        "mistral": "#fb923c",
        "minimax": "#fb7185",
        "qwen": "#a3e635",
        "zhipu": "#e879f9",
        "moonshot": "#22d3ee",
        "xai": "#9aa4b2",
        "cohere": "#a78bfa",
        "amazon": "#e0b24c",
        "microsoft": "#7ea6ff",
        "nvidia": "#4ade80",
    },
    "family_fallback": ("#2dd4bf", "#7aa2ff", "#fb7185", "#f0b429", "#c084fc", "#22d3ee"),
}

THEMES: dict[str, dict[str, Any]] = {"light": LIGHT, "dark": DARK}

#: 当前生效的调色板。tkinter 是在构造控件时读颜色的，所以换主题只能是「换掉这个
#: 引用 + 重建控件树」；任何跨重建缓存了颜色值的地方都会渲染成旧主题。
PALETTE: dict[str, Any] = LIGHT


def _missing_roles(theme: dict[str, Any]) -> list[str]:
    return [role for role in _CHROME_ROLES if role not in theme]


for _name, _theme in THEMES.items():
    if _missing_roles(_theme):
        raise RuntimeError(f"主题 {_name!r} 缺少角色: {_missing_roles(_theme)}")
del _name, _theme


def verdict_style(verdict: str) -> tuple[str, str, str]:
    """``(前景色, 背景色, 给小白看的一句话)``，认不出来的 verdict 走中性卡片。"""
    fg, bg = PALETTE["verdict"].get(verdict, PALETTE["verdict_fallback"])
    return fg, bg, _VERDICT_TEXT.get(verdict, "")


def verdict_text(verdict: str, default: str = "见报告。") -> str:
    """只有文案，不要颜色 —— 纯文本报告和剪贴板走这条。"""
    return _VERDICT_TEXT.get(verdict, default)


def fail_style() -> tuple[str, str, str]:
    fg, bg = PALETTE["fail"]
    return fg, bg, _FAIL_TEXT


def severity_fill(key: str) -> tuple[str, str]:
    """一个**计数不为零**的 severity 胶囊的配色。计数为零的胶囊不填色。"""
    fills = PALETTE["severity_fill"]
    return fills.get(key, fills["info"])


_FONT = "Microsoft YaHei UI"
_MONO_FONT = "Consolas"



def family_color(family: str) -> str | None:
    """Tint for one self-reported family name, or ``None`` for "nothing to tint".

    ``None`` is not an error case: an empty name means the model produced no
    usable self-report, and there is no claim on the line to colour.
    """
    name = family.strip().lower()
    if not name:
        return None
    tinted = PALETTE["family"]
    if name in tinted:
        return tinted[name]
    # Deterministic mixing (position-weighted), so "minimax2" and "minimax" do not
    # land on the same fallback slot.
    digest = sum((i + 1) * ord(ch) for i, ch in enumerate(name))
    fallback = PALETTE["family_fallback"]
    return fallback[digest % len(fallback)]


def _shade(color: str, factor: float) -> str:
    """Darken a ``#rrggbb`` colour towards black by ``factor``.

    Used for the card's hairline rules. The card background is one of five tints
    depending on the verdict, so a fixed grey rule clashes on some of them, and a
    rule that fights its own background is worse than no rule at all.
    """
    try:
        parts = [int(color[i : i + 2], 16) for i in (1, 3, 5)]
    except (ValueError, IndexError):
        return PALETTE["shade_fallback"]
    return "#" + "".join(f"{max(0, min(255, int(c * factor))):02x}" for c in parts)


def _mix_color(start: str, end: str, amount: float) -> str:
    """Blend two ``#rrggbb`` colours for short, non-blocking UI transitions."""
    try:
        left = [int(start[i : i + 2], 16) for i in (1, 3, 5)]
        right = [int(end[i : i + 2], 16) for i in (1, 3, 5)]
    except (ValueError, IndexError):
        return end
    amount = min(1.0, max(0.0, amount))
    return "#" + "".join(
        f"{round(a + (b - a) * amount):02x}" for a, b in zip(left, right)
    )

#: How the identity probe's per-model verdict is worded on the card. The probe's
#: own vocabulary is ``contradiction`` / ``unstable`` / ``unrechecked`` /
#: ``consistent`` / ``unchecked`` (``relaycheck/probes/identity.py``), and the
#: wording here has to carry the same weight: a self-report is a lead, never a
#: conclusion. ``unchecked`` doubles as the fallback for a verdict this shell has
#: never seen, so an upstream rename degrades to "no usable self-report" rather
#: than to a blank line.
_FAMILY_VERDICT_TEXT: dict[str, str] = {
    "contradiction": "售卖 {expected}，自称 {reported}",
    "consistent": "售卖 {expected}，自称 {reported}（一致）",
    "unstable": "同一个问题问两遍，自称的厂商不一样 —— 不采信",
    "unrechecked": "自称与售卖名称不同，但没来得及复核 —— 不下结论",
    "unchecked": "没拿到可用的自述",
}

#: Display column the quoted self-report starts at, so the quotes stack instead
#: of stair-stepping with the length of the family name above them.
_QUOTE_COLUMN = 34


def _display_width(text: str) -> int:
    """Columns a string occupies in a terminal-ish font, CJK counted double.

    Needed because the family block is laid out by hand: Tk falls back to a
    proportional font for the CJK runs inside the Consolas label, so ``len()``
    does not predict where the next column lands. A rough wide/narrow split is
    accurate enough to line the quotes up.
    """
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _split_template(key: str, expected: str, reported: str) -> tuple[str, str, str]:
    """Split a ``_FAMILY_VERDICT_TEXT`` template around its ``{reported}`` slot.

    Partitioning the template instead of restating the sentence here keeps the
    wording in exactly one place, and guarantees the tinted piece is the same
    substring the plain-text line shows.
    """
    head, _slot, tail = _FAMILY_VERDICT_TEXT[key].partition("{reported}")
    return head.format(expected=expected), reported, tail


def _family_rows(report: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(prefix, reported, suffix)`` per model — the card's colouring hook.

    ``prefix + reported + suffix`` is exactly the line ``_family_lines`` returns,
    so the plain-text form and the tinted form are rendered from one source and
    cannot drift apart.

    Only the middle piece is tinted, and only by vendor. Tinting the whole row
    would also tint 「售卖 deepseek」 — the name being *checked* — with the colour of
    the claim being *made*, which inverts what the colour means. The middle piece
    is empty when the model produced no usable self-report: there is no claim on
    the line, so there is nothing to tint.

    Why this exists at all: the identity probe's per-model claims used to reach
    the window only as a finding *title* (「模型自称的厂商与售卖名称不一致」), with the
    claims themselves and the quote that produced them left in ``report.json``.
    Showing them is what makes the tool's own caveat checkable rather than merely
    stated: the reader sees that the "wrong" answer is stock "I was created by
    OpenAI" boilerplate coming off a DeepSeek endpoint, and understands *why* a
    self-report cannot carry a verdict. A caveat the reader can verify is worth
    more than one they must take on trust.

    Quotes come from the ``id-100`` finding's own evidence rather than being
    re-derived here, so the window can never pick a different sentence than the
    report did.
    """
    quotes: dict[str, str] = {}
    for finding in report.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        evidence = finding.get("evidence") or {}
        for item in evidence.get("contradictions") or []:
            if isinstance(item, dict) and item.get("model"):
                quotes[str(item["model"])] = str(item.get("quote") or "")

    # Contradictions first — the interesting ones must not sit below the fold.
    order = {"contradiction": 0, "unstable": 1, "unrechecked": 2, "consistent": 3}

    for result in report.get("results") or []:
        if not isinstance(result, dict) or result.get("probe") != "identity":
            continue
        observations = (result.get("data") or {}).get("observations") or {}
        if not isinstance(observations, dict):
            continue
        ranked = sorted(
            ((str(m), r) for m, r in observations.items() if isinstance(r, dict)),
            key=lambda kv: (order.get(str(kv[1].get("verdict")), 9), kv[0]),
        )
        rows: list[tuple[str, str, str, str, str]] = []
        for model, record in ranked:
            if record.get("error"):
                rows.append(("?", model, "自述采集失败", "", ""))
                continue
            verdict = str(record.get("verdict") or "unchecked")
            expected = str(record.get("expected_family") or "未知")
            reported = (
                "、".join(str(x) for x in (record.get("confirmed_families") or []))
                or "、".join(str(x) for x in (record.get("self_reported_families") or []))
                or "未知"
            )
            if verdict == "contradiction":
                mark = "!"
                head, tinted, tail = _split_template("contradiction", expected, reported)
                quote = quotes.get(model)
                if quote:
                    pad = " " * max(
                        2, _QUOTE_COLUMN - _display_width(head + tinted + tail)
                    )
                    tail += f"{pad}「{quote}」"
            elif verdict == "consistent":
                mark = "="
                # When the probe recorded no family at all, fall back to the sales
                # name rather than printing 「自称 未知（一致）」, which contradicts
                # itself.
                agreed = expected if reported == "未知" else reported
                head, tinted, tail = _split_template("consistent", expected, agreed)
            else:
                mark = "?"
                head = _FAMILY_VERDICT_TEXT.get(verdict, _FAMILY_VERDICT_TEXT["unchecked"])
                tinted, tail = "", ""
            rows.append((mark, model, head, tinted, tail))

        # Pad the model name to the batch width. Consolas is monospace for ASCII
        # but Tk substitutes a *proportional* font for the CJK runs, so the 售卖
        # column only lines up if the one field that is always ASCII and always
        # differs in length is padded by hand. Without this the block
        # stair-steps and reads as broken text rather than as a table.
        width = min(max((len(m) for _, m, _, _, _ in rows), default=0), 26)
        out: list[tuple[str, str, str]] = []
        for mark, model, head, tinted, tail in rows:
            pad = " " * (width - len(model)) if len(model) <= width else ""
            out.append((f"  {mark} {model}{pad}  {head}", tinted, tail))
        return out
    return []


def _family_lines(report: dict[str, Any]) -> list[str]:
    """``_family_rows`` flattened to plain text — one line per model."""
    return [
        prefix + reported + suffix for prefix, reported, suffix in _family_rows(report)
    ]


def _top_finding_line(findings: Sequence[dict[str, Any]]) -> str:
    """Summarise the worst actual problem, never a CLEAN/INFO record.

    ``report.json`` carries verified-normal records and non-accusatory clues in the
    same sorted list as problems. Calling either one "最严重的一条" on a clean card
    contradicts the verdict above it, so this summary is reserved for severities
    the reporter itself treats as a problem.
    """
    problem_levels = {"critical", "high", "medium", "low"}
    for finding in findings:
        if str(finding.get("severity") or "").lower() not in problem_levels:
            continue
        return f"\n最严重的一条：{finding.get('id', '')}  {finding.get('title', '')}"
    return ""


# ------------------------------------------------------------------- process glue

def _child_command(args: Sequence[str]) -> list[str]:
    """Build the command that runs one audit.

    From source we re-enter the installed CLI module. Frozen there is no module to
    re-enter, so we fall back to the sibling console build, and finally to asking
    this very executable to re-dispatch (``--run-audit``) — which is also what the
    development build does, since the GUI script is importable either way.
    """
    if getattr(sys, "frozen", False):
        sibling = Path(sys.executable).with_name("relaycheck.exe")
        if sibling.is_file():
            return [str(sibling), *args]
        return [sys.executable, "--run-audit", *args]
    return [sys.executable, "-m", "relaycheck.cli", *args]


class AuditProcess:
    """One audit run in a child process, with line-by-line output streaming."""

    def __init__(self, args: Sequence[str], api_key: str) -> None:
        self.args = list(args)
        self.api_key = api_key
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.returncode: int | None = None
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        env = dict(os.environ)
        # Passed in the environment, never on the command line (see module docstring).
        env["RELAYCHECK_API_KEY"] = self.api_key
        # Force UTF-8 on the pipe. The child would otherwise encode its Chinese
        # progress lines with the console code page, and the GUI would show
        # mojibake for the entire run.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"

        kwargs: dict[str, Any] = {}
        if os.name == "nt":  # keep the console-subsystem child from flashing a window
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        self._proc = subprocess.Popen(
            _child_command(self.args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **kwargs,
        )
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            self.lines.put(line.rstrip("\n"))
        self._proc.wait()
        self.returncode = self._proc.returncode
        self.lines.put(None)  # sentinel: no more output

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        if self.running and self._proc is not None:
            self._proc.terminate()


class ExactProgress(tk.Frame):
    """A theme-independent progress bar that never invents completion.

    Windows' ``vista`` theme paints ``ttk.Progressbar`` green even when a named
    style asks for another colour. Green is semantic here — it means CLEAN — so
    running chrome must not borrow it. This widget keeps the small configuration
    surface the application and tests use while drawing only CLI-reported values.
    """

    def __init__(self, parent: tk.Misc, length: int = 220) -> None:
        super().__init__(
            parent, width=length, height=8, background=PALETTE["track"],
            bd=0, highlightthickness=0,
        )
        self._maximum = 1.0
        self._value = 0.0
        self._fill = tk.Frame(self, background=PALETTE["progress_fill"], bd=0)
        self.pack_propagate(False)

    def _redraw(self) -> None:
        ratio = 0.0 if self._maximum <= 0 else min(
            1.0, max(0.0, self._value / self._maximum)
        )
        if ratio <= 0:
            self._fill.place_forget()
        else:
            self._fill.place(x=0, y=0, relheight=1, relwidth=ratio)

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        options = dict(cnf or {})
        options.update(kwargs)
        if "maximum" in options:
            self._maximum = float(options.pop("maximum"))
        if "value" in options:
            self._value = float(options.pop("value"))
        result = super().configure(**options) if options else None
        self._redraw()
        return result

    config = configure

    def cget(self, key: str) -> Any:
        if key == "maximum":
            return self._maximum
        if key == "value":
            return self._value
        return super().cget(key)

    def __getitem__(self, key: str) -> Any:
        return self.cget(key)


# ---------------------------------------------------------------- window chrome

def _icon_path(suffix: str) -> Path | None:
    """Locate ``relaycheck.<suffix>``, frozen or from source.

    PyInstaller unpacks bundled data under ``sys._MEIPASS``; running this script
    directly the asset sits next to it. Returns ``None`` when neither exists, so a
    missing icon degrades to the Tk default instead of refusing to start — an
    icon is not worth a window that will not open.
    """
    candidates: list[Path] = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / f"relaycheck.{suffix}")
    candidates.append(Path(__file__).resolve().parent / f"relaycheck.{suffix}")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# ------------------------------------------------------------------------- the UI

class RelayCheckApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.proc: AuditProcess | None = None
        self.last_out_dir: Path | None = None
        self._model_names: list[str] = []
        self._probes_done = 0
        self._probes_total = 0
        self._icon_image: tk.PhotoImage | None = None
        self._icon_error = ""

        root.title(f"{APP_TITLE} {__version__}")
        root.geometry("980x800")
        root.minsize(840, 660)
        root.configure(background=PALETTE["app_bg"])
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._apply_icon()
        self._configure_styles()

        # A vertical pane split so the log can be dragged taller. The form has a
        # fixed height and the log is the only thing here that grows without bound,
        # so on a small window the log is what gets squeezed — and the log is where
        # the evidence lands.
        #
        # Both panes exist before anything packs into them: ``PanedWindow.add``
        # sizes a pane from its child, so the child has to be in the tree first.
        self.paned = ttk.PanedWindow(root, orient="vertical", style="Relay.TPanedwindow")
        self.paned.pack(fill="both", expand=True)
        self.top = tk.Frame(self.paned, background=PALETTE["app_bg"], bd=0)
        self.paned.add(self.top, weight=1)

        # On a maximised monitor a form stretched over 1900px becomes harder to
        # scan, not more spacious.  Keep the working column readable and centre it;
        # the minimum window still gets a safe 20px gutter.
        self.content = tk.Frame(self.top, background=PALETTE["app_bg"], bd=0)
        self.content.pack(fill="x", padx=20)
        self._content_pad = 20
        self.top.bind("<Configure>", self._on_top_resize)

        self._build_header()
        self._build_form()
        self._build_controls()
        self._build_verdict()
        self._build_log()

        self._set_running(False)
        self._log(
            "填上中转站地址和 API Key，点「开始检测」就行。\n"
            "模型那一栏可以留空 —— 留空会自动从 /v1/models 里挑几个不同厂商的来对比。\n"
        )
        # The divider can only be placed once the paned window has been laid out.
        # Called from here it is a silent no-op: ``sashpos`` accepts the number,
        # discards it because the widget is 1px tall, and the window then opens with
        # the top pane — the entire form — collapsed to nothing while the log holds
        # the whole window. So it goes on the first real layout instead, once.
        self._sash_placed = False
        self.paned.bind("<Configure>", self._on_paned_configure)

    def _configure_styles(self) -> None:
        """Configure named ttk styles without replacing the native theme.

        ``theme_use`` is intentionally absent: changing the global theme would
        mutate every native control and re-open several Windows-only layout bugs.
        Named styles let the few ttk controls we keep share the new palette while
        leaving the platform integration alone.
        """
        style = ttk.Style(self.root)
        style.configure("Relay.TPanedwindow", background=PALETTE["app_bg"])
        style.configure(
            "Relay.Horizontal.TProgressbar",
            troughcolor=PALETTE["track"],
            background=PALETTE["progress_fill"],
            lightcolor=PALETTE["progress_fill"],
            darkcolor=PALETTE["progress_fill"],
            bordercolor=PALETTE["border"],
        )
        style.configure(
            "Advanced.TRadiobutton",
            background=PALETTE["surface_subtle"],
            foreground=PALETTE["text"],
            font=(_FONT, 9),
        )
        style.map(
            "Advanced.TRadiobutton",
            background=[("active", PALETTE["surface_subtle"])],
        )

    def _on_top_resize(self, event: tk.Event) -> None:
        """Centre the readable column without making height screen-dependent."""
        pad = max(20, (int(event.width) - 1180) // 2)
        if pad != self._content_pad:
            self._content_pad = pad
            self.content.pack_configure(padx=pad)

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        kind: str = "secondary",
        compact: bool = False,
    ) -> tk.Button:
        """Create one flat, keyboard-focusable button with a real visual role."""
        palettes = {
            "primary": (PALETTE["accent"], PALETTE["on_accent"], PALETTE["accent_hover"]),
            "secondary": (PALETTE["button_secondary_bg"], PALETTE["text"], PALETTE["button_secondary_hover"]),
            "danger": (PALETTE["danger_soft"], PALETTE["danger"], PALETTE["button_danger_hover"]),
            "ghost": (PALETTE["app_bg"], PALETTE["text_muted"], PALETTE["accent_soft"]),
            "card": (PALETTE["surface"], PALETTE["text"], PALETTE["accent_soft"]),
        }
        bg, fg, active = palettes[kind]
        button = tk.Button(
            parent,
            text=text,
            command=command,
            font=(_FONT, 9, "bold" if kind == "primary" else "normal"),
            background=bg,
            foreground=fg,
            activebackground=active,
            activeforeground=fg,
            disabledforeground=PALETTE["text_faint"],
            relief="flat",
            bd=0,
            padx=12 if compact else 18,
            pady=5 if compact else 8,
            cursor="hand2",
            highlightthickness=1 if kind == "card" else 0,
            highlightbackground=PALETTE["border"],
            highlightcolor=PALETTE["accent"],
            takefocus=True,
        )
        button._relay_palette = (bg, fg, active)  # type: ignore[attr-defined]
        button._relay_anim_token = 0  # type: ignore[attr-defined]

        def animate(target: str) -> None:
            # The audit loop already drains output every 120ms. During a run, keep
            # hover feedback immediate and avoid scheduling cosmetic repaint work.
            if self.proc is not None and self.proc.running:
                button.configure(background=target)
                return
            button._relay_anim_token += 1  # type: ignore[attr-defined]
            token = button._relay_anim_token  # type: ignore[attr-defined]
            start = str(button.cget("background"))

            def frame(step: int) -> None:
                if (
                    token != button._relay_anim_token  # type: ignore[attr-defined]
                    or str(button["state"]) != "normal"
                ):
                    return
                button.configure(background=_mix_color(start, target, step / 6))
                if step < 6:
                    button.after(15, lambda: frame(step + 1))

            frame(1)

        def on_enter(_event: tk.Event) -> None:
            if str(button["state"]) == "normal":
                animate(active)

        def on_leave(_event: tk.Event) -> None:
            if str(button["state"]) == "normal":
                animate(bg)

        button.bind("<Enter>", on_enter)
        button.bind("<Leave>", on_leave)
        return button

    def _entry(
        self,
        parent: tk.Misc,
        variable: tk.StringVar,
        show: str | None = None,
        width: int | None = None,
    ) -> tk.Entry:
        options: dict[str, Any] = {
            "textvariable": variable,
            "font": (_FONT, 10),
            "background": PALETTE["surface_subtle"],
            "foreground": PALETTE["text"],
            "insertbackground": PALETTE["text"],
            "relief": "flat",
            "bd": 0,
            "highlightthickness": 1,
            "highlightbackground": PALETTE["border"],
            "highlightcolor": PALETTE["accent"],
        }
        if show is not None:
            options["show"] = show
        if width is not None:
            options["width"] = width
        return tk.Entry(parent, **options)

    def _field_heading(self, parent: tk.Misc, title: str, hint: str = "") -> tk.Frame:
        row = tk.Frame(parent, background=PALETTE["surface"], bd=0)
        tk.Label(
            row, text=title, background=PALETTE["surface"], foreground=PALETTE["text"],
            font=(_FONT, 9, "bold"),
        ).pack(side="left")
        if hint:
            tk.Label(
                row, text=hint, background=PALETTE["surface"], foreground=PALETTE["text_faint"],
                font=(_FONT, 9),
            ).pack(side="right")
        return row

    def _set_button_enabled(self, button: tk.Button, enabled: bool) -> None:
        """Keep custom buttons visually honest when their state changes."""
        button._relay_anim_token += 1  # type: ignore[attr-defined]
        bg, fg, _active = button._relay_palette  # type: ignore[attr-defined]
        if enabled:
            button.configure(
                state="normal", background=bg, foreground=fg, cursor="hand2"
            )
        else:
            button.configure(
                state="disabled", background=PALETTE["button_disabled_bg"],
                foreground=PALETTE["text_faint"], cursor="arrow",
            )

    def _apply_icon(self) -> None:
        path = _icon_path("png")
        self._icon_error = "" if path is not None else "找不到图标文件"
        if path is None:
            return
        # Two attempts, because one is not reliable. In a long-lived process that builds
        # several windows, `image create photo -file` was observed to fail with an empty
        # TclError about once in fifteen runs — no message, no ::errorInfo — and then
        # succeed immediately when asked again. A real failure (unreadable or corrupt
        # PNG) fails both times and is reported below rather than swallowed.
        for _ in range(2):
            try:
                # Held on self deliberately: Tk does not own the image, and a
                # garbage-collected PhotoImage leaves the window with a blank icon.
                self._icon_image = tk.PhotoImage(file=str(path))
                break
            except tk.TclError as exc:
                self._icon_error = f"{type(exc).__name__}: {str(exc) or 'Tk 没给原因'}"
        if self._icon_image is None:
            return
        try:
            self.root.iconphoto(True, self._icon_image)
        except tk.TclError as exc:
            # A window without an icon still opens, so this is not fatal — but it must
            # not be silent either. Swallowing it is exactly how the icon went missing
            # the first time: nothing anywhere said the load had failed.
            self._icon_image = None
            self._icon_error = f"iconphoto {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------ widgets

    def _build_header(self) -> None:
        head = tk.Frame(self.content, background=PALETTE["app_bg"], bd=0)
        head.pack(fill="x", pady=(18, 14))

        brand = tk.Frame(head, background=PALETTE["app_bg"], bd=0)
        brand.pack(side="left", fill="x", expand=True)
        title_row = tk.Frame(brand, background=PALETTE["app_bg"], bd=0)
        title_row.pack(anchor="w")
        tk.Label(
            title_row, text="relaycheck", background=PALETTE["app_bg"], foreground=PALETTE["text"],
            font=(_FONT, 18, "bold"),
        ).pack(side="left")
        tk.Label(
            title_row, text=f"桌面版  {__version__}", background=PALETTE["accent_soft"],
            foreground=PALETTE["accent"], font=(_FONT, 8, "bold"), padx=8, pady=3,
        ).pack(side="left", padx=(10, 0), pady=(3, 0))
        tk.Label(
            brand,
            text="用可复现证据检查模型掉包与计费异常",
            background=PALETTE["app_bg"], foreground=PALETTE["text_muted"], font=(_FONT, 9),
        ).pack(anchor="w", pady=(4, 0))

        trust = tk.Frame(head, background=PALETTE["app_bg"], bd=0)
        trust.pack(side="right", anchor="e")
        for text in ("只读审计", "Key 不落盘"):
            tk.Label(
                trust, text=text, background=PALETTE["surface"], foreground=PALETTE["text_muted"],
                font=(_FONT, 8, "bold"), padx=10, pady=5,
                highlightthickness=1, highlightbackground=PALETTE["border_soft"],
            ).pack(side="left", padx=(8, 0))

    def _build_form(self) -> None:
        box = tk.Frame(
            self.content, background=PALETTE["surface"], bd=0,
            highlightthickness=1, highlightbackground=PALETTE["border_soft"],
        )
        box.pack(fill="x")

        card_head = tk.Frame(box, background=PALETTE["surface"], bd=0)
        card_head.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 12))
        tk.Label(
            card_head, text="检测目标", background=PALETTE["surface"], foreground=PALETTE["text"],
            font=(_FONT, 11, "bold"),
        ).pack(side="left")
        tk.Label(
            card_head, text="支持 OpenAI 兼容接口", background=PALETTE["surface"],
            foreground=PALETTE["text_faint"], font=(_FONT, 9),
        ).pack(side="right")

        body = tk.Frame(box, background=PALETTE["surface"], bd=0)
        body.grid(row=1, column=0, sticky="ew", padx=18, pady=(0, 16))
        body.columnconfigure(0, weight=1, uniform="field")
        body.columnconfigure(1, weight=1, uniform="field")
        box.columnconfigure(0, weight=1)

        self.url_var = tk.StringVar()
        self.key_var = tk.StringVar()
        self.models_var = tk.StringVar()
        self.outdir_var = tk.StringVar(value=str(self._default_out_dir()))
        self.strength_var = tk.StringVar(value="default")
        self.maxmodels_var = tk.StringVar(value="6")
        self.timeout_var = tk.StringVar(value="60")
        self.budget_var = tk.StringVar(value="240")

        top_fields = tk.Frame(body, background=PALETTE["surface"], bd=0)
        top_fields.grid(row=0, column=0, columnspan=2, sticky="ew")
        top_fields.columnconfigure(0, weight=1, uniform="top-field")
        top_fields.columnconfigure(1, weight=1, uniform="top-field")

        url_block = tk.Frame(top_fields, background=PALETTE["surface"], bd=0)
        url_block.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._field_heading(url_block, "中转站地址", "例如 https://api.example.com").pack(fill="x")
        self._entry(url_block, self.url_var).pack(fill="x", ipady=7, pady=(6, 0))

        key_block = tk.Frame(top_fields, background=PALETTE["surface"], bd=0)
        key_block.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self._field_heading(key_block, "API Key", "仅驻留内存").pack(fill="x")
        key_row = tk.Frame(key_block, background=PALETTE["surface"], bd=0)
        key_row.pack(fill="x", pady=(6, 0))
        key_row.columnconfigure(0, weight=1)
        self.key_entry = self._entry(key_row, self.key_var, show="●")
        self.key_entry.grid(row=0, column=0, sticky="ew", ipady=7)
        self.show_key = tk.BooleanVar(value=False)
        tk.Checkbutton(
            key_row, text="显示", variable=self.show_key, command=self._toggle_key,
            background=PALETTE["surface"], activebackground=PALETTE["surface"], foreground=PALETTE["text_muted"],
            selectcolor=PALETTE["surface"], font=(_FONT, 9), bd=0, highlightthickness=0,
        ).grid(row=0, column=1, padx=(10, 0))

        bottom_fields = tk.Frame(body, background=PALETTE["surface"], bd=0)
        bottom_fields.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        bottom_fields.columnconfigure(0, weight=4)
        bottom_fields.columnconfigure(1, weight=6)

        model_block = tk.Frame(bottom_fields, background=PALETTE["surface"], bd=0)
        model_block.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._field_heading(model_block, "模型（可选）", "留空将自动挑选").pack(fill="x")
        models_row = tk.Frame(model_block, background=PALETTE["surface"], bd=0)
        models_row.pack(fill="x", pady=(6, 0))
        models_row.columnconfigure(0, weight=1)
        self._entry(models_row, self.models_var).grid(row=0, column=0, sticky="ew", ipady=7)
        self.fetch_btn = self._button(
            models_row, "获取列表", self._fetch_models, kind="secondary", compact=True
        )
        self.fetch_btn.grid(row=0, column=1, padx=(8, 0), sticky="ns")

        out_block = tk.Frame(bottom_fields, background=PALETTE["surface"], bd=0)
        out_block.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self._field_heading(out_block, "报告输出目录", "完成后可直接打开").pack(fill="x")
        out_row = tk.Frame(out_block, background=PALETTE["surface"], bd=0)
        out_row.pack(fill="x", pady=(6, 0))
        out_row.columnconfigure(0, weight=1)
        self._entry(out_row, self.outdir_var).grid(row=0, column=0, sticky="ew", ipady=7)
        self._button(
            out_row, "浏览…", self._pick_outdir, kind="secondary", compact=True
        ).grid(row=0, column=1, padx=(8, 0), sticky="ns")

        self.model_list_frame = tk.Frame(body, background=PALETTE["surface"], bd=0)
        self.model_list_frame.grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0)
        )
        self.model_list_frame.columnconfigure(0, weight=1)
        self.model_list = tk.Listbox(
            self.model_list_frame,
            selectmode="extended", height=5, exportselection=False,
            background=PALETTE["surface_subtle"], foreground=PALETTE["text"],
            selectbackground=PALETTE["accent_soft"], selectforeground=PALETTE["accent"],
            font=(_FONT, 9), relief="flat", bd=0,
            highlightthickness=1, highlightbackground=PALETTE["border"],
        )
        self.model_list.grid(row=0, column=0, sticky="ew")
        self.model_list_scroll = ttk.Scrollbar(
            self.model_list_frame, orient="vertical", command=self.model_list.yview
        )
        self.model_list_scroll.grid(row=0, column=1, sticky="ns")
        self.model_list.configure(yscrollcommand=self.model_list_scroll.set)
        self.model_list_frame.grid_remove()  # only appears once a list is fetched

        # ------------------------------------------------------------- advanced
        # Collapsed by default. These three settings have correct defaults for
        # nearly every run, and showing them costs a third of the form's height —
        # height that the result card needs as soon as a run comes back with
        # family lines in it.
        self.adv_open = tk.BooleanVar(value=False)
        self.adv_toggle = self._button(
            body, _ADV_CLOSED, self._toggle_advanced, kind="card", compact=True
        )
        self.adv_toggle.grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )

        self.adv_panel = tk.Frame(
            body, background=PALETTE["surface_subtle"], bd=0,
            highlightthickness=1, highlightbackground=PALETTE["border_soft"],
        )
        self.adv_panel.grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )

        tk.Label(
            self.adv_panel, text="检测强度", background=PALETTE["surface_subtle"],
            foreground=PALETTE["text"], font=(_FONT, 9, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(12, 10), pady=(10, 0))
        ttk.Radiobutton(
            self.adv_panel, text="标准（默认 8 项探针）", value="default",
            variable=self.strength_var, style="Advanced.TRadiobutton",
        ).grid(row=0, column=1, sticky="w", pady=(10, 0))
        ttk.Radiobutton(
            self.adv_panel, text="全面（全部 11 项，更慢、也更贵）",
            value="all", variable=self.strength_var, style="Advanced.TRadiobutton",
        ).grid(row=0, column=2, sticky="w", pady=(10, 0), padx=(12, 12))

        # 「更贵」必须写清楚贵在哪里，否则这个选项只是在吓人。多出来的三项里
        # params / stream 很便宜，真正花钱的是 context：它按升序阶梯发送上万 token 的
        # 输入，一次就可能比其余 10 项加起来还贵。它默认不跑，省下的不是时间，是使用者的钱。
        tk.Label(
            self.adv_panel,
            text=(
                "多出来的三项里，长输入完整性每次要发上万 token —— 一次就可能比其余 10 项"
                "加起来还贵，所以默认不跑。"
            ),
            background=PALETTE["surface_subtle"], foreground=PALETTE["text_muted"],
            font=(_FONT, 9), wraplength=880, justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=12, pady=(6, 0))

        nums = tk.Frame(self.adv_panel, background=PALETTE["surface_subtle"], bd=0)
        nums.grid(row=2, column=0, columnspan=3, sticky="w", padx=12, pady=(10, 12))
        tk.Label(
            nums, text="最多测几个模型", background=PALETTE["surface_subtle"],
            foreground=PALETTE["text"], font=(_FONT, 9),
        ).pack(side="left")
        ttk.Spinbox(nums, from_=1, to=20, width=4, textvariable=self.maxmodels_var).pack(
            side="left", padx=(6, 16)
        )
        tk.Label(
            nums, text="单次请求超时（秒）", background=PALETTE["surface_subtle"],
            foreground=PALETTE["text"], font=(_FONT, 9),
        ).pack(side="left")
        self._entry(nums, self.timeout_var, width=5).pack(side="left", padx=(6, 16), ipady=3)
        tk.Label(
            nums, text="每项探针预算（秒）", background=PALETTE["surface_subtle"],
            foreground=PALETTE["text"], font=(_FONT, 9),
        ).pack(side="left")
        self._entry(nums, self.budget_var, width=6).pack(side="left", padx=(6, 0), ipady=3)

        self.adv_panel.grid_remove()

    def _build_controls(self) -> None:
        bar = tk.Frame(self.content, background=PALETTE["app_bg"], bd=0)
        bar.pack(fill="x", pady=(12, 0))

        self.start_btn = self._button(bar, "开始检测", self._start, kind="primary")
        self.start_btn.pack(side="left")
        self.stop_btn = self._button(bar, "停止", self._stop, kind="danger")
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.open_btn = self._button(bar, "打开报告", self._open_out_dir, kind="secondary")
        self.open_btn.pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="空闲")
        state = tk.Frame(
            bar, background=PALETTE["surface"], bd=0, padx=10, pady=8,
            highlightthickness=1, highlightbackground=PALETTE["border_soft"],
        )
        state.pack(side="right", anchor="e")
        self.status_dot = tk.Label(
            state, text="●", background=PALETTE["surface"], foreground=PALETTE["text_faint"],
            font=(_FONT, 8),
        )
        self.status_dot.pack(side="left", padx=(0, 7))
        tk.Label(
            state, textvariable=self.status_var, background=PALETTE["surface"],
            foreground=PALETTE["text_muted"], font=(_FONT, 9),
        ).pack(side="left", padx=(0, 12))

        # Progress. Until this existed the only sign of life during a four-minute
        # audit against a dead relay was the log scrolling — which is precisely the
        # moment a user concludes the program has hung and kills it.
        self.progress = ExactProgress(state, length=220)
        self.progress.pack(side="left")

        # The spend warning used to sit at the far right of this row, at body size
        # and the same weight as everything else, so the one line on screen that
        # costs money was also the easiest to ignore. It gets its own row directly
        # under the button it warns about, in bold.
        warn = tk.Frame(self.content, background=PALETTE["app_bg"], bd=0)
        warn.pack(fill="x", pady=(8, 4))
        tk.Label(
            warn,
            text="⚠  检测会消耗你自己的 API 额度（一般几十到上百次请求）。",
            background=PALETTE["app_bg"],
            foreground=PALETTE["spend_warn"],
            font=(_FONT, 9, "bold"),
        ).pack(anchor="w")

    def _add_rule(self, parent: tk.Misc) -> tk.Frame:
        """A 1px hairline that takes its colour from the card underneath it.

        ``ttk.Separator`` is the obvious choice and the wrong one: ttk widgets
        ignore ``background``, so on a tinted card (``#fee2e2`` and friends) it
        keeps the theme's grey and reads as a stray line from another window.
        """
        rule = tk.Frame(parent, height=1, bd=0, background=PALETTE["rule"])
        rule.pack(fill="x", padx=(20, 16), pady=0)
        return rule

    def _build_verdict(self) -> None:
        self.card = tk.Frame(
            self.content, bd=0, relief="flat", background=PALETTE["surface"],
            highlightthickness=1, highlightbackground=PALETTE["border_soft"],
        )
        self.card.pack(fill="x", pady=(6, 0))
        # A narrow status rail gives the eye a target without turning the entire
        # card into a loud verdict banner.  ``place`` keeps the rail outside the
        # pack order that the result-section tests deliberately assert on.
        self.card_accent = tk.Frame(self.card, width=4, background=PALETTE["border"])
        self.card_accent.place(x=0, y=0, relheight=1)

        # ---- 结论
        self.card_header = tk.Frame(self.card, background=PALETTE["surface"], bd=0)
        self.card_header.pack(fill="x")
        self.verdict_label = tk.Label(
            self.card_header, text="等待开始", font=(_FONT, 14, "bold"),
            foreground=PALETTE["text"], background=PALETTE["surface"], anchor="w", justify="left",
        )
        self.verdict_label.pack(fill="x", padx=(20, 16), pady=(14, 3))
        self.verdict_detail = tk.Label(
            self.card_header, text="填写目标并开始检测，结论与证据会出现在这里。",
            background=PALETTE["surface"], foreground=PALETTE["text_muted"], anchor="w", justify="left",
            font=(_FONT, 9),
            wraplength=900,
        )
        self.verdict_detail.pack(fill="x", padx=(20, 16), pady=(0, 14))

        # ---- 问题数量
        # Each section carries its own leading rule, so hiding a section hides its
        # rule with it instead of leaving two rules stacked with nothing between.
        self.chips_section = tk.Frame(self.card, background=PALETTE["surface"])
        self.chips_section.pack(fill="x")
        self.chips_rule = self._add_rule(self.chips_section)
        tk.Label(
            self.chips_section, text="问题数量", background=PALETTE["surface"], anchor="w",
            font=(_FONT, 9, "bold"), foreground=PALETTE["text_muted"],
        ).pack(fill="x", padx=(20, 16), pady=(10, 5))
        chips_holder = tk.Frame(self.chips_section, background=PALETTE["surface"])
        chips_holder.pack(anchor="w", padx=(20, 16), pady=(0, 10))
        self.chips: dict[str, tk.Label] = {}
        for key, tag in _SEVERITY_CHIPS:
            chip = tk.Label(
                chips_holder, text=f"{tag} 0", font=(_MONO_FONT, 9, "bold"), bd=0,
                padx=8, pady=2, background=PALETTE["surface"], foreground=PALETTE["severity_empty_fg"],
            )
            chip.pack(side="left", padx=(0, 5))
            self.chips[key] = chip

        # ---- 家族线索
        self.family_section = tk.Frame(self.card, background=PALETTE["surface"])
        self.family_section.pack(fill="x")
        self.family_rule = self._add_rule(self.family_section)
        self.family_caption = tk.Label(
            self.family_section,
            text="家族线索：每个模型自己供出的身份（只是线索，单独一条不足以定性）",
            background=PALETTE["surface"], anchor="w", justify="left", wraplength=900,
            font=(_FONT, 9, "bold"), foreground=PALETTE["text_muted"],
        )
        self.family_caption.pack(fill="x", padx=(20, 16), pady=(10, 5))
        # The per-model self-reports. Deliberately part of the card and not of the
        # log: the log is raw tool output and scrolls away, whereas "who did each
        # model say it was" is the evidence for the caveat printed right above it.
        #
        # A ``Text`` and not a ``Label``: tinting the self-reported vendor means
        # tinting part of a line, and a Label carries one colour for the whole
        # string. What the widget costs in fiddle it pays back in selection — the
        # quote is the evidence, and people paste evidence.
        self.family_text = tk.Text(
            self.family_section, height=1, font=(_MONO_FONT, 9), bd=0,
            highlightthickness=0, background=PALETTE["surface"], foreground=PALETTE["text"],
            wrap="char", cursor="arrow", takefocus=0,
        )
        self.family_text.pack(fill="x", padx=(20, 16), pady=(0, 12))
        self.family_text.configure(state="disabled")

        # ---- 动作
        # On the card and not only in the toolbar: the verdict is the thing someone
        # wants to send to the vendor, and the moment they want it is the moment
        # they are reading it. The toolbar button stays as the way to reach the
        # folder before any run has produced one.
        self.action_section = tk.Frame(self.card, background=PALETTE["surface"])
        self.action_section.pack(fill="x")
        self.action_rule = self._add_rule(self.action_section)
        actions = tk.Frame(self.action_section, background=PALETTE["surface"])
        actions.pack(anchor="w", padx=(20, 16), pady=(10, 14))
        self.copy_btn = self._button(
            actions, "复制结论", self._copy_card, kind="card", compact=True
        )
        self.copy_btn.pack(side="left")
        self.card_open_btn = self._button(
            actions, "打开报告", self._open_out_dir, kind="card", compact=True
        )
        self.card_open_btn.pack(side="left", padx=(8, 0))
        self.copy_hint = tk.Label(
            actions, text="", background=PALETTE["surface"], foreground=PALETTE["text_muted"],
            font=(_FONT, 9),
        )
        self.copy_hint.pack(side="left", padx=(10, 0))

        # Nothing has been measured yet, so none of these sections has anything
        # true to say. A row of six zeros would read as "we measured zero
        # problems", which is a claim this window has not earned.
        self._set_sections(show_chips=False, show_family=False, show_actions=False)

    def _set_sections(
        self, show_chips: bool, show_family: bool, show_actions: bool = False
    ) -> None:
        """Show or hide the card's lower sections, preserving their order.

        ``pack`` always appends, so the order has to be re-established by hand —
        otherwise a second run could come back with the family clue sitting above
        the problem counts, or the buttons above both of them.
        """
        self.chips_section.pack_forget()
        self.family_section.pack_forget()
        self.action_section.pack_forget()
        if show_family:
            self.family_section.pack(fill="x")
        if show_chips:
            if show_family:
                self.chips_section.pack(fill="x", before=self.family_section)
            else:
                self.chips_section.pack(fill="x")
        if show_actions:
            self.action_section.pack(fill="x")

    def _build_log(self) -> None:
        # A pane takes no padding of its own, so the margins the log used to get
        # from ``pack`` live in a plain frame that is itself the pane. No top margin
        # on that frame: the hairline below has to land on the divider, and the log
        # frame brings its own gap.
        outer = tk.Frame(self.paned, background=PALETTE["app_bg"], bd=0)
        self.paned.add(outer, weight=3)
        # Under this theme the sash is painted in the same colour as everything
        # around it, so an untouched divider is invisible and nobody finds out the
        # log can be pulled taller. This is the entire affordance.
        self.sash_hint = tk.Frame(outer, height=3, background=PALETTE["sash_hint"])
        self.sash_hint.pack(fill="x", padx=20)
        self.log_wrap = tk.Frame(
            outer, background=PALETTE["log_bg"], bd=0,
            highlightthickness=1, highlightbackground=PALETTE["log_border"],
        )
        self.log_wrap.pack(fill="both", expand=True, padx=20, pady=(10, 16))
        log_head = tk.Frame(self.log_wrap, background=PALETTE["log_head_bg"], bd=0)
        log_head.pack(fill="x", padx=12, pady=(9, 7))
        tk.Label(
            log_head, text="运行详情", background=PALETTE["log_head_bg"], foreground=PALETTE["log_head_fg"],
            font=(_FONT, 9, "bold"),
        ).pack(side="left")
        tk.Label(
            log_head, text="拖动上方分隔线可调整高度", background=PALETTE["log_head_bg"],
            foreground=PALETTE["log_hint_fg"], font=(_FONT, 8),
        ).pack(side="right")
        self.log = ScrolledText(
            self.log_wrap, wrap="word", height=9, font=(_MONO_FONT, 9),
            state="disabled", background=PALETTE["log_text_bg"], foreground=PALETTE["log_text_fg"],
            insertbackground=PALETTE["log_caret"], selectbackground=PALETTE["log_select_bg"],
            selectforeground=PALETTE["log_select_fg"], relief="flat", bd=0,
            highlightthickness=0, padx=10, pady=8,
        )
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._log_pad = 20
        outer.bind("<Configure>", self._on_log_resize)

    # ------------------------------------------------------------------- the sash

    def _on_log_resize(self, event: tk.Event) -> None:
        """Use the same readable-width gutter for the log pane."""
        pad = max(20, (int(event.width) - 1180) // 2)
        if pad != self._log_pad:
            self._log_pad = pad
            self.sash_hint.pack_configure(padx=pad)
            self.log_wrap.pack_configure(padx=pad)

    def _on_paned_configure(self, event: tk.Event) -> None:
        """Place the divider the first time the paned window has a real size."""
        if self._sash_placed or event.height <= 1:
            return
        self._sash_placed = True
        self._reset_sash()

    def _reset_sash(self) -> None:
        """Put the divider where the form ends.

        Without this the log opens at whatever height ttk guessed from the two
        panes' requested sizes, which on a tall window is usually most of it.
        """
        try:
            self.paned.sashpos(0, self.top.winfo_reqheight())
        except tk.TclError:
            pass

    def _fit_sash(self) -> None:
        """Never let the divider clip the card.

        The card grows once a run comes back with family lines in it, and a divider
        dragged up on an empty card would cut them off. A clipped family block is
        worse than no block at all: it reads as if that were everything there was.
        Only ever pushes the divider *down*, so a deliberate choice is left alone.
        """
        self.top.update_idletasks()
        # Read once: the value moves while the pane is being laid out, and comparing
        # against one reading while setting another lets the divider land past the
        # content it was supposed to fit.
        need = self.top.winfo_reqheight()
        try:
            if self.paned.sashpos(0) < need:
                self.paned.sashpos(0, need)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ helpers

    def _default_out_dir(self) -> Path:
        base = Path.home() / "Documents"
        if not base.is_dir():
            base = Path.home()
        import time

        return base / "relaycheck报告" / time.strftime("%Y%m%d-%H%M%S")

    def _toggle_key(self) -> None:
        self.key_entry.configure(show="" if self.show_key.get() else "●")

    def _toggle_advanced(self) -> None:
        """Open or close the 高级 panel.

        ``grid_remove`` and not ``grid_forget``: remove remembers the grid options,
        so the panel comes back on its own row with its own span instead of the
        position having to be written down twice and drift apart.
        """
        opening = not self.adv_open.get()
        self.adv_open.set(opening)
        if opening:
            self.adv_panel.grid()
        else:
            self.adv_panel.grid_remove()
        self.adv_toggle.configure(text=_ADV_OPEN if opening else _ADV_CLOSED)
        # Opening the panel makes the pane taller, and the divider does not move on
        # its own: the card below would silently lose its bottom row — the 「复制结论」
        # button — to a pane edge the user never touched.
        self._fit_sash()

    def _pick_outdir(self) -> None:
        chosen = filedialog.askdirectory(title="选择报告输出目录")
        if chosen:
            self.outdir_var.set(chosen)

    def _log(self, text: str) -> None:
        """Append to the log pane, with the API key scrubbed out first.

        The CLI never prints the key, but a proxy or an unexpected traceback could
        echo a header. The window is screenshot-able and the log is the one place
        raw tool output lands, so scrub defensively rather than trust upstream.
        """
        secret = self.key_var.get().strip()
        if secret:
            text = text.replace(secret, "***")
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        for widget in (self.start_btn, self.fetch_btn):
            self._set_button_enabled(widget, not running)
        self._set_button_enabled(self.stop_btn, running)
        self.status_dot.configure(foreground=PALETTE["accent"] if running else PALETTE["text_faint"])
        # Both open buttons follow the same rule: there is exactly one report
        # directory, so two buttons disagreeing about whether it exists is a bug.
        can_open = bool(self.last_out_dir and self.last_out_dir.is_dir())
        for button in (self.open_btn, self.card_open_btn):
            self._set_button_enabled(button, can_open)

    def _set_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.configure(maximum=total, value=min(done, total))
        else:
            self.progress.configure(maximum=1, value=0)

    def _note_progress(self, line: str) -> None:
        """Advance the bar from the CLI's own progress lines.

        The child is a separate process whose output format we do not own, so this
        only acts on lines it fully recognises, and it never *guesses* completion.
        If the run is stopped or crashes, the bar stops where the evidence stopped
        — filling it anyway would be this window telling a lie the CLI never told.
        """
        text = line.strip()
        if text.startswith("探针: "):
            names = [n for n in text[len("探针: "):].split(",") if n.strip()]
            self._probes_total = len(names)
            self._set_progress(self._probes_done, self._probes_total)
        elif text.startswith("→ "):
            name = text[len("→ "):].rstrip(" …")
            if name:
                self.status_var.set(
                    f"第 {self._probes_done + 1}/{self._probes_total} 项：{name}"
                    if self._probes_total
                    else f"正在跑：{name}"
                )
        elif text.startswith("√ "):
            self._probes_done += 1
            self._set_progress(self._probes_done, self._probes_total)
            if self._probes_total:
                self.status_var.set(f"已完成 {self._probes_done}/{self._probes_total} 项")

    def _tint(self, bg: str) -> None:
        """Tint the conclusion header while leaving evidence on a neutral surface.

        A full green card reads as a certificate of innocence; a full red one reads
        as a judgement.  Both overstate what a family-level audit can say.  The
        semantic tint therefore identifies the conclusion, while counts, evidence,
        and hand-off controls stay on white.  Chips keep their own fill semantics.
        """

        def paint(widget: tk.Misc, colour: str) -> None:
            if widget.winfo_class() != "Button" and not widget.winfo_class().startswith("T"):
                try:
                    widget.configure(background=colour)
                except tk.TclError:
                    pass
            for child in widget.winfo_children():
                paint(child, colour)

        self.card.configure(background=PALETTE["surface"])
        paint(self.card_header, bg)
        for section in (self.chips_section, self.family_section, self.action_section):
            paint(section, PALETTE["surface"])
        self.card.configure(highlightbackground=_shade(bg, 0.88))
        for rule in (self.chips_rule, self.family_rule, self.action_rule):
            rule.configure(background=PALETTE["border_soft"])

    def _fill_family(self, rows: Sequence[tuple[str, str, str]]) -> None:
        """Render the family block, tinting each self-reported vendor.

        Only the reported piece is tinted. Tinting the whole line would also tint
        「售卖 deepseek」 — the name being *checked* — in the colour of the claim being
        *made*, which inverts what the colour means. Same vendor, same colour, so
        "both of these answered OpenAI" is visible without reading every line.

        The colour is identity grouping, never guilt: it says those two claims are
        the same claim, not that either model is a fake. The caption above the
        block keeps saying so, because colour is exactly where a lead gets
        mistaken for a verdict.
        """
        widget = self.family_text
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for index, (prefix, reported, suffix) in enumerate(rows):
            widget.insert("end", prefix)
            tint = family_color(reported)
            if tint and reported:
                tag = f"fam:{index}"
                widget.tag_configure(tag, foreground=tint)
                widget.insert("end", reported, tag)
            widget.insert("end", suffix)
            if index != len(rows) - 1:
                widget.insert("end", "\n")
        # Height follows the content, so a one-model run does not leave a block of
        # blank monospace under the card. Counted as *display* lines rather than as
        # rows, because a long quote wraps and a row count would clip the tail of
        # it — silently cutting off the evidence the block exists to show.
        height = len(rows)
        try:
            widget.update_idletasks()
            # A widget that has not been laid out yet reports one display line per
            # character, which would inflate the card to the length of the quote.
            # Only trust the count once it knows its own width.
            if rows and widget.winfo_width() > 1:
                counted = widget.count("1.0", "end-1c", "displaylines")
                height = int(counted[0] if isinstance(counted, tuple) else counted)
        except (tk.TclError, TypeError, ValueError):
            pass
        widget.configure(height=max(1, height))
        widget.configure(state="disabled")

    def _card_text(self) -> str:
        """The whole card as plain text — the handle the tests assert on, and the
        body of what 「复制结论」 hands over.

        The card is a dozen widgets now; asserting on thirteen separate ``cget``
        calls would pin the layout instead of the content. Sections that are
        hidden contribute nothing, so this reads as what the user sees.
        """
        parts = [self.verdict_label["text"]]
        if self.verdict_detail["text"]:
            parts.append(self.verdict_detail["text"])
        if self.chips_section.winfo_manager():
            parts.append(
                "   ".join(
                    f"{tag}={self.chips[key]['text'].split()[-1]}"
                    for key, tag in _SEVERITY_CHIPS
                )
            )
        if self.family_section.winfo_manager():
            parts.append(self.family_caption["text"])
            content = self.family_text.get("1.0", "end-1c")
            if content:
                parts.append(content)
        return "\n".join(part for part in parts if part)

    def _clipboard_text(self) -> str:
        """What 「复制结论」 hands over.

        The card on its own lands in a chat with no idea which relay it is about or
        when it ran, which is the difference between a complaint someone can act on
        and a screenshot. The footer is assembled from the URL field and the report
        path only — never from the key field — so this window cannot leak the
        secret even by accident. ``_card_text`` stays footer-free so the tests keep
        asserting on the card itself.
        """
        parts = [self._card_text()]
        footer: list[str] = []
        url = self.url_var.get().strip()
        if url:
            footer.append(f"站点：{url}")
        if self.last_out_dir and (self.last_out_dir / "report.json").is_file():
            footer.append(f"报告：{self.last_out_dir}")
        if footer:
            footer.append(f"工具：relaycheck 桌面版 {__version__}")
            parts.append("——\n" + "\n".join(footer))
        return "\n".join(part for part in parts if part)

    def _copy_card(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self._clipboard_text())

        def clear() -> None:
            try:
                self.copy_hint.configure(text="")
            except tk.TclError:
                pass

        # Said out loud, because a copy button that looks like it did nothing is a
        # button people press twice and then stop trusting. Cleared again so the
        # confirmation cannot be mistaken for the state of the next card.
        self.copy_hint.configure(text="已复制到剪贴板")
        self.root.after(2500, clear)

    def _show_card(
        self,
        verdict: str,
        detail: str,
        counts: dict[str, int] | None,
        family_rows: Sequence[tuple[str, str, str]] | None = None,
        actions: bool = False,
    ) -> None:
        fg, bg, _ = verdict_style(verdict)
        self._tint(bg)
        self.card_accent.configure(background=fg)
        self.verdict_label.configure(text=verdict, foreground=fg)
        self.verdict_detail.configure(text=detail, foreground=PALETTE["text"])

        # A chip is filled only when its count is non-zero: a solid red
        # ``CRITICAL 0`` on a run that found nothing reads as an alarm, which is
        # the opposite of what the card says. Filled means "this happened"; grey
        # means "this did not".
        for key, tag in _SEVERITY_CHIPS:
            count = (counts or {}).get(key, 0)
            fill_bg, fill_fg = severity_fill(key)
            self.chips[key].configure(
                text=f"{tag} {count}",
                background=fill_bg if count > 0 else PALETTE["surface"],
                foreground=fill_fg if count > 0 else PALETTE["severity_empty_fg"],
            )

        rows = list(family_rows or ())
        # Sections first, then content: the family Text sizes itself from its own
        # laid-out width, and a widget that is still unpacked has none.
        self._set_sections(bool(counts), bool(rows), actions)
        self._fill_family(rows)
        self.root.update_idletasks()
        self._fit_sash()

    def _open_out_dir(self) -> None:
        if not (self.last_out_dir and self.last_out_dir.is_dir()):
            return
        target = self.last_out_dir
        try:
            if os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            messagebox.showwarning(APP_TITLE, f"打不开目录：{exc}")

    def _on_close(self) -> None:
        if self.proc is not None and self.proc.running:
            if not messagebox.askokcancel(APP_TITLE, "检测还在跑，确定要关掉吗？"):
                return
            self.proc.stop()
        self.root.destroy()

    # ---------------------------------------------------------------- the audit

    def _fetch_models(self) -> None:
        url = self.url_var.get().strip()
        key = self.key_var.get().strip()
        if not url or not key:
            messagebox.showwarning(APP_TITLE, "先把中转站地址和 API Key 填上。")
            return
        self._set_button_enabled(self.fetch_btn, False)
        self.status_dot.configure(foreground=PALETTE["accent"])
        self.status_var.set("正在获取模型列表…")

        def work() -> None:
            try:
                names = RelayClient(url, key, timeout=25.0).list_models()
                error = None
            except (RelayError, Exception) as exc:  # noqa: BLE001 - report, never crash the GUI
                names, error = [], f"{type(exc).__name__}: {exc}"
            self.root.after(0, lambda: self._models_fetched(names, error))

        threading.Thread(target=work, daemon=True).start()

    def _models_fetched(self, names: list[str], error: str | None) -> None:
        self._set_button_enabled(self.fetch_btn, True)
        self.status_dot.configure(foreground=PALETTE["text_faint"])
        self.status_var.set("空闲")
        if error:
            self._log(f"获取模型列表失败：{error}")
            messagebox.showwarning(
                APP_TITLE,
                f"没能拿到模型列表：\n{error}\n\n不影响使用 —— 模型栏留空会自动挑，"
                "也可以自己手填。",
            )
            return
        if not names:
            self._log("这个站没有返回任何模型。")
            return
        self._model_names = names
        self.model_list.delete(0, "end")
        for name in names:
            self.model_list.insert("end", name)
        self.model_list_frame.grid()
        self._log(f"拿到 {len(names)} 个模型，按住 Ctrl 可以点选多个。")

    def _collect_models(self) -> str:
        """Selected listbox entries win; otherwise fall back to the text field."""
        picked = [self.model_list.get(i) for i in self.model_list.curselection()]
        if picked:
            return ",".join(picked)
        return self.models_var.get().strip()

    def _start(self) -> None:
        url = self.url_var.get().strip()
        key = self.key_var.get().strip()
        if not url:
            messagebox.showwarning(APP_TITLE, "请填中转站地址。")
            return
        if not key:
            messagebox.showwarning(APP_TITLE, "请填 API Key。")
            return
        if not url.lower().startswith(("http://", "https://")):
            if not messagebox.askokcancel(
                APP_TITLE, f"地址看起来不像网址：\n{url}\n\n还是按 https://{url} 试？"
            ):
                return
            url = "https://" + url
            self.url_var.set(url)

        out_dir = Path(self.outdir_var.get().strip() or str(self._default_out_dir()))
        # An absolute --out-dir is mandatory here: a GUI process has no meaningful
        # working directory, so a relative path would scatter reports somewhere the
        # user will never find (or fail outright in a read-only cwd).
        out_dir = out_dir.expanduser()
        if not out_dir.is_absolute():
            out_dir = Path.home() / out_dir

        args: list[str] = ["-u", url, "--out-dir", str(out_dir)]
        models = self._collect_models()
        if models:
            args += ["-m", models]
        args += ["--max-models", self.maxmodels_var.get().strip() or "6"]
        args += ["--timeout", self.timeout_var.get().strip() or "60"]
        args += ["--budget", self.budget_var.get().strip() or "240"]
        if self.strength_var.get() == "all":
            args += ["--probes", "all"]

        self.last_out_dir = out_dir
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._show_card("正在检测…", "已启动，输出会实时显示在下面。", None)
        self._log(f"命令等价于：relaycheck -u {url} --out-dir {out_dir} "
                  f"{'-m ' + models if models else '(自动挑选模型)'}")

        self._probes_done = 0
        self._probes_total = 0
        self._set_progress(0, 0)
        self.proc = AuditProcess(args, key)
        self.proc.start()
        self._set_running(True)
        self.status_var.set("检测中…")
        self.root.after(120, self._drain)

    def _stop(self) -> None:
        if self.proc is not None and self.proc.running:
            self.proc.stop()
            self._log("已请求停止，正在等待子进程退出…")

    def _drain(self) -> None:
        assert self.proc is not None
        done = False
        while True:
            try:
                line = self.proc.lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                done = True
                break
            self._note_progress(line)
            self._log(line)

        if done:
            self._finish()
            return
        self.root.after(120, self._drain)

    def _finish(self) -> None:
        assert self.proc is not None
        code = self.proc.returncode
        self._set_running(False)
        if self._probes_total and self._probes_done < self._probes_total:
            # Deliberately does not top the bar up. A run that was stopped, or one
            # whose probe budget ran out, did not finish the checklist, and a full
            # bar would claim it did.
            self.status_var.set(
                f"提前结束（{self._probes_done}/{self._probes_total} 项，退出码 {code}）"
            )
        else:
            self.status_var.set(f"结束（退出码 {code}）")

        report = None
        if self.last_out_dir is not None:
            candidate = self.last_out_dir / "report.json"
            if candidate.is_file():
                try:
                    report = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    self._log(f"报告读不出来：{exc}")

        if report is None:
            fg, bg, detail = fail_style()
            if code in (0, 1):
                # Exit 0/1 mean the audit ran; no report.json means it was killed
                # before writing. Saying "运行失败" without that nuance would hide
                # the difference between "the relay is clean" and "we stopped it".
                detail = ("检测被中断，报告没写出来。没有报告不代表中转站有问题，"
                          "也不代表没问题 —— 只代表这次没查完。")
            self._show_card("运行失败", detail, None, actions=True)
            self._tint(bg)
            self.card_accent.configure(background=fg)
            self.verdict_label.configure(foreground=fg)
            return

        verdict = str(report.get("verdict") or "运行结束")
        counts = report.get("severity_counts") or {}
        plain = verdict_text(verdict)
        findings = report.get("findings") or []
        top = _top_finding_line(findings)
        self._show_card(verdict, plain + top, counts, _family_rows(report), actions=True)
        self._log("")
        self._log(f"报告：{self.last_out_dir / 'report.md'}")
        self._log(f"原始：{self.last_out_dir / 'report.json'}")
        self._log(f"请求 {report.get('requests_made', '?')} 次，"
                  f"耗时 {report.get('duration_s', '?')}s")
        self._set_button_enabled(self.open_btn, True)
        self._set_button_enabled(self.card_open_btn, True)


def _repair_standard_streams() -> None:
    """Make ``print`` work when a windowed bundle starts us as the audit engine.

    Two separate problems, both caused by the app being frozen:

    1. PyInstaller's windowed bootloader sets ``sys.stdout`` / ``sys.stderr`` to
       ``None`` because no console is attached. The only way this branch is reached
       is the GUI starting us *with stdout redirected to a pipe*, so the underlying
       file descriptors are perfectly valid — wrap them again or every ``print`` in
       ``relaycheck.cli`` raises ``AttributeError: 'NoneType' object has no
       attribute 'write'``.
    2. A frozen app ignores ``PYTHONIOENCODING``, so the repaired stream would
       inherit the system code page (cp936 here) and the GUI — which decodes the
       pipe as UTF-8 — would show a screenful of mojibake even though the report on
       disk is correct. ``force_utf8_output`` is the same fix the standalone engine
       uses; one implementation, both entry points.
    """
    for name, fd in (("stdout", 1), ("stderr", 2)):
        if getattr(sys, name, None) is not None:
            continue
        try:
            stream = open(fd, "w", encoding="utf-8", errors="replace",
                          buffering=1, closefd=False)
        except OSError:
            stream = open(os.devnull, "w", encoding="utf-8")
        setattr(sys, name, stream)
    from relaycheck.cli import force_utf8_output

    force_utf8_output()


def main(argv: Sequence[str] | None = None) -> int:
    """GUI entry point.

    Also serves as the frozen re-dispatch target: when PyInstaller bundles this
    script there may be no separate ``relaycheck.exe`` to call, so the GUI
    executable re-enters itself with ``--run-audit`` and behaves as the plain CLI
    for that child process. The decision is made before Tk is ever touched, so the
    child stays headless.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--run-audit":
        _repair_standard_streams()
        from relaycheck.cli import main as cli_main

        return cli_main(args[1:])

    root = tk.Tk()
    RelayCheckApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
