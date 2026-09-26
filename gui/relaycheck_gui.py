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

#: ``(前景色, 背景色, 给小白看的一句话)`` keyed by the CLI's own verdict string.
#: Kept in sync with ``reporter.Report.verdict``; an unknown verdict falls back to
#: a neutral card rather than crashing the window after a 3-minute audit.
VERDICT_STYLE: dict[str, tuple[str, str, str]] = {
    "检测到可直接定性的掉包证据": (
        "#7f1d1d", "#fee2e2",
        "被测站返回了无法用正常行为解释的证据。可以把这个目录里的 report.md "
        "直接发给商家对质。",
    ),
    "检测到高风险问题": (
        "#7f1d1d", "#fee2e2",
        "发现了值得认真对待的问题。请打开 report.md 看具体是哪几条。",
    ),
    "检测到中等问题": (
        "#78350f", "#fef3c7",
        "发现了异常，但单独一条不足以定性。建议结合报告自行复核。",
    ),
    "仅检测到轻微问题": (
        "#713f12", "#fef9c3",
        "只有轻微异常。多数情况是正常转售带来的副作用，不构成指控。",
    ),
    "未检测到问题": (
        "#14532d", "#dcfce7",
        "本次没有发现达到阈值的问题。注意：这只代表「这次没查出来」，"
        "不等于「这家站一定没问题」—— 本工具只能给到家族级线索。",
    ),
}

_FAIL_STYLE = (
    "#7f1d1d", "#fee2e2",
    "这次没查成，没有生成报告。这不代表中转站有问题，也不代表没问题 —— "
    "只代表这一次没查成。下面是原始输出。",
)

_SEVERITY_CHIPS: tuple[tuple[str, str], ...] = (
    ("critical", "CRITICAL"),
    ("high", "HIGH"),
    ("medium", "MEDIUM"),
    ("low", "LOW"),
    ("info", "INFO"),
    ("clean", "CLEAN"),
)

#: ``(底色, 字色)`` for a severity chip **whose count is not zero**. A chip that
#: reads zero is deliberately left unfilled and greyed instead: ``CRITICAL=0`` on a
#: solid red badge is an alarm, and on a clean run it is the exact opposite of what
#: the report says. Filled means "this happened"; grey means "this did not".
_SEVERITY_FILL: dict[str, tuple[str, str]] = {
    "critical": ("#b91c1c", "#ffffff"),
    "high": ("#c2410c", "#ffffff"),
    "medium": ("#a16207", "#ffffff"),
    "low": ("#4d7c0f", "#ffffff"),
    "info": ("#475569", "#ffffff"),
    "clean": ("#15803d", "#ffffff"),
}

#: Grey for a zero-count chip. Every card background is light (see
#: ``VERDICT_STYLE``), so one mid-dark grey stays legible on all of them and reads
#: as inactive next to a white-on-colour filled chip.
_SEVERITY_EMPTY_FG = "#78716c"

#: Vendor → colour. Colouring the *self-reported* family is the whole point of the
#: tinting: two models that both answer "I was created by OpenAI" light up the same
#: colour, which is what makes the stock-boilerplate tell visible at a glance
#: instead of something the reader has to notice by reading every line.
#:
#: This is identity grouping, **not** guilt. A tint says "these two claims are the
#: same claim", never "this model is a fake". The card's own caption still carries
#: the caveat, because colour is exactly where a lead gets mistaken for a verdict.
_FAMILY_COLORS: dict[str, str] = {
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
}

#: Used for a family this build has never heard of, so a new vendor still gets a
#: stable tint instead of no tint. Picked by a deterministic hash, never by
#: ``hash()`` — that is salted per process, and the same report would then come out
#: in different colours on every run.
_FAMILY_FALLBACK = ("#0f766e", "#1d4ed8", "#be123c", "#a16207", "#7c3aed", "#0e7490")


def family_color(family: str) -> str | None:
    """Tint for one self-reported family name, or ``None`` for "nothing to tint".

    ``None`` is not an error case: an empty name means the model produced no
    usable self-report, and there is no claim on the line to colour.
    """
    name = family.strip().lower()
    if not name:
        return None
    if name in _FAMILY_COLORS:
        return _FAMILY_COLORS[name]
    # Deterministic mixing (position-weighted), so "minimax2" and "minimax" do not
    # land on the same fallback slot.
    digest = sum((i + 1) * ord(ch) for i, ch in enumerate(name))
    return _FAMILY_FALLBACK[digest % len(_FAMILY_FALLBACK)]


def _shade(color: str, factor: float) -> str:
    """Darken a ``#rrggbb`` colour towards black by ``factor``.

    Used for the card's hairline rules. The card background is one of five tints
    depending on the verdict, so a fixed grey rule clashes on some of them, and a
    rule that fights its own background is worse than no rule at all.
    """
    try:
        parts = [int(color[i : i + 2], 16) for i in (1, 3, 5)]
    except (ValueError, IndexError):
        return "#dddddd"
    return "#" + "".join(f"{max(0, min(255, int(c * factor))):02x}" for c in parts)

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
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._apply_icon()

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
        head = ttk.Frame(self.root, padding=(16, 14, 16, 4))
        head.pack(fill="x")
        ttk.Label(
            head, text=f"relaycheck 桌面版 {__version__}",
            font=("Microsoft YaHei UI", 15, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            head,
            text="检测 LLM API 中转站是否掉包模型、是否虚报计费。全程只读：不改你的账号，"
                 "不建号，不动配额。",
            foreground="#555555",
        ).pack(anchor="w", pady=(2, 0))

    def _build_form(self) -> None:
        box = ttk.LabelFrame(self.root, text="目标", padding=(12, 8, 12, 12))
        box.pack(fill="x", padx=16, pady=(8, 0))
        box.columnconfigure(1, weight=1)

        self.url_var = tk.StringVar()
        self.key_var = tk.StringVar()
        self.models_var = tk.StringVar()
        self.outdir_var = tk.StringVar(value=str(self._default_out_dir()))
        self.strength_var = tk.StringVar(value="default")
        self.maxmodels_var = tk.StringVar(value="6")
        self.timeout_var = tk.StringVar(value="60")
        self.budget_var = tk.StringVar(value="240")

        r = 0
        ttk.Label(box, text="中转站地址").grid(row=r, column=0, sticky="w", pady=4, padx=(0, 8))
        ttk.Entry(box, textvariable=self.url_var).grid(row=r, column=1, sticky="ew", pady=4)
        ttk.Label(box, text="例：https://api.example.com", foreground="#777777").grid(
            row=r, column=2, sticky="w", padx=(8, 0)
        )

        r += 1
        ttk.Label(box, text="API Key").grid(row=r, column=0, sticky="w", pady=4, padx=(0, 8))
        key_row = ttk.Frame(box)
        key_row.grid(row=r, column=1, sticky="ew", pady=4)
        key_row.columnconfigure(0, weight=1)
        self.key_entry = ttk.Entry(key_row, textvariable=self.key_var, show="●")
        self.key_entry.grid(row=0, column=0, sticky="ew")
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            key_row, text="显示", variable=self.show_key, command=self._toggle_key
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Label(box, text="只存在内存里，不落盘", foreground="#777777").grid(
            row=r, column=2, sticky="w", padx=(8, 0)
        )

        r += 1
        ttk.Label(box, text="模型（可选）").grid(row=r, column=0, sticky="w", pady=4, padx=(0, 8))
        models_row = ttk.Frame(box)
        models_row.grid(row=r, column=1, sticky="ew", pady=4)
        models_row.columnconfigure(0, weight=1)
        ttk.Entry(models_row, textvariable=self.models_var).grid(row=0, column=0, sticky="ew")
        self.fetch_btn = ttk.Button(models_row, text="获取模型列表", command=self._fetch_models)
        self.fetch_btn.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(box, text="留空＝自动挑选", foreground="#777777").grid(
            row=r, column=2, sticky="w", padx=(8, 0)
        )

        r += 1
        self.model_list_frame = ttk.Frame(box)
        self.model_list_frame.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(2, 4))
        self.model_list_frame.columnconfigure(0, weight=1)
        self.model_list = tk.Listbox(
            self.model_list_frame, selectmode="extended", height=5, exportselection=False
        )
        self.model_list.grid(row=0, column=0, sticky="ew")
        self.model_list_scroll = ttk.Scrollbar(
            self.model_list_frame, orient="vertical", command=self.model_list.yview
        )
        self.model_list_scroll.grid(row=0, column=1, sticky="ns")
        self.model_list.configure(yscrollcommand=self.model_list_scroll.set)
        self.model_list_frame.grid_remove()  # only appears once a list is fetched
        ttk.Label(
            box,
            text="（按住 Ctrl 点选多个。选中的会覆盖上面那栏 —— 挑不同厂商的才有对比价值）",
            foreground="#777777",
        ).grid(row=r + 1, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.model_hint = self.model_list_frame.grid_info()  # remembered for re-show

        r += 2
        ttk.Label(box, text="输出目录").grid(row=r, column=0, sticky="w", pady=4, padx=(0, 8))
        out_row = ttk.Frame(box)
        out_row.grid(row=r, column=1, sticky="ew", pady=4)
        out_row.columnconfigure(0, weight=1)
        ttk.Entry(out_row, textvariable=self.outdir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(out_row, text="浏览…", command=self._pick_outdir).grid(row=0, column=1, padx=(8, 0))
        ttk.Label(box, text="报告写在这里", foreground="#777777").grid(
            row=r, column=2, sticky="w", padx=(8, 0)
        )

        # ------------------------------------------------------------- advanced
        r += 1
        adv = ttk.LabelFrame(box, text="高级（不确定就别动）", padding=(10, 6, 10, 8))
        adv.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        ttk.Label(adv, text="检测强度").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Radiobutton(
            adv, text="标准（默认 8 项探针）", value="default", variable=self.strength_var
        ).grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(
            adv, text="全面（全部 11 项，请求数明显更多、更慢）",
            value="all", variable=self.strength_var,
        ).grid(row=0, column=2, sticky="w")

        nums = ttk.Frame(adv)
        nums.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(nums, text="最多测几个模型").pack(side="left")
        ttk.Spinbox(nums, from_=1, to=20, width=4, textvariable=self.maxmodels_var).pack(
            side="left", padx=(6, 16)
        )
        ttk.Label(nums, text="单次请求超时(秒)").pack(side="left")
        ttk.Entry(nums, width=5, textvariable=self.timeout_var).pack(side="left", padx=(6, 16))
        ttk.Label(nums, text="每项探针预算(秒)").pack(side="left")
        ttk.Entry(nums, width=6, textvariable=self.budget_var).pack(side="left", padx=(6, 0))

    def _build_controls(self) -> None:
        bar = ttk.Frame(self.root, padding=(16, 10, 16, 4))
        bar.pack(fill="x")

        self.start_btn = ttk.Button(bar, text="开始检测", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="停止", command=self._stop)
        self.stop_btn.pack(side="left", padx=(8, 0))
        self.open_btn = ttk.Button(bar, text="打开报告目录", command=self._open_out_dir)
        self.open_btn.pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="空闲")
        ttk.Label(bar, textvariable=self.status_var, foreground="#555555").pack(
            side="left", padx=(16, 0)
        )

        # Progress. Until this existed the only sign of life during a four-minute
        # audit against a dead relay was the log scrolling — which is precisely the
        # moment a user concludes the program has hung and kills it.
        self.progress = ttk.Progressbar(bar, mode="determinate", length=200)
        self.progress.pack(side="right")

        # The spend warning used to sit at the far right of this row, at body size
        # and the same weight as everything else, so the one line on screen that
        # costs money was also the easiest to ignore. It gets its own row directly
        # under the button it warns about, in bold.
        warn = ttk.Frame(self.root, padding=(16, 0, 16, 4))
        warn.pack(fill="x")
        ttk.Label(
            warn,
            text="⚠ 检测会消耗你自己的 API 额度（一般几十到上百次请求）。",
            foreground="#a1541a",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor="w")

    def _add_rule(self, parent: tk.Misc) -> tk.Frame:
        """A 1px hairline that takes its colour from the card underneath it.

        ``ttk.Separator`` is the obvious choice and the wrong one: ttk widgets
        ignore ``background``, so on a tinted card (``#fee2e2`` and friends) it
        keeps the theme's grey and reads as a stray line from another window.
        """
        rule = tk.Frame(parent, height=1, bd=0, background="#dddddd")
        rule.pack(fill="x", padx=12, pady=0)
        return rule

    def _build_verdict(self) -> None:
        self.card = tk.Frame(self.root, bd=1, relief="solid", background="#eeeeee")
        self.card.pack(fill="x", padx=16, pady=(6, 0))

        # ---- 结论
        self.verdict_label = tk.Label(
            self.card, text="还没跑过", font=("Microsoft YaHei UI", 13, "bold"),
            background="#eeeeee", anchor="w", justify="left",
        )
        self.verdict_label.pack(fill="x", padx=12, pady=(10, 2))
        self.verdict_detail = tk.Label(
            self.card, text="", background="#eeeeee", anchor="w", justify="left",
            wraplength=900,
        )
        self.verdict_detail.pack(fill="x", padx=12, pady=(0, 8))

        # ---- 问题数量
        # Each section carries its own leading rule, so hiding a section hides its
        # rule with it instead of leaving two rules stacked with nothing between.
        self.chips_section = tk.Frame(self.card, background="#eeeeee")
        self.chips_section.pack(fill="x")
        self.chips_rule = self._add_rule(self.chips_section)
        tk.Label(
            self.chips_section, text="问题数量", background="#eeeeee", anchor="w",
            font=("Microsoft YaHei UI", 9), foreground="#6b7280",
        ).pack(fill="x", padx=12, pady=(8, 3))
        chips_holder = tk.Frame(self.chips_section, background="#eeeeee")
        chips_holder.pack(anchor="w", padx=12, pady=(0, 8))
        self.chips: dict[str, tk.Label] = {}
        for key, tag in _SEVERITY_CHIPS:
            chip = tk.Label(
                chips_holder, text=f"{tag} 0", font=("Consolas", 9, "bold"), bd=0,
                padx=7, pady=1, background="#eeeeee", foreground=_SEVERITY_EMPTY_FG,
            )
            chip.pack(side="left", padx=(0, 5))
            self.chips[key] = chip

        # ---- 家族线索
        self.family_section = tk.Frame(self.card, background="#eeeeee")
        self.family_section.pack(fill="x")
        self.family_rule = self._add_rule(self.family_section)
        self.family_caption = tk.Label(
            self.family_section,
            text="家族线索：每个模型自己供出的身份（只是线索，单独一条不足以定性）",
            background="#eeeeee", anchor="w", justify="left", wraplength=900,
            font=("Microsoft YaHei UI", 9), foreground="#6b7280",
        )
        self.family_caption.pack(fill="x", padx=12, pady=(8, 3))
        # The per-model self-reports. Deliberately part of the card and not of the
        # log: the log is raw tool output and scrolls away, whereas "who did each
        # model say it was" is the evidence for the caveat printed right above it.
        #
        # A ``Text`` and not a ``Label``: tinting the self-reported vendor means
        # tinting part of a line, and a Label carries one colour for the whole
        # string. What the widget costs in fiddle it pays back in selection — the
        # quote is the evidence, and people paste evidence.
        self.family_text = tk.Text(
            self.family_section, height=1, font=("Consolas", 9), bd=0,
            highlightthickness=0, background="#eeeeee", foreground="#333333",
            wrap="char", cursor="arrow", takefocus=0,
        )
        self.family_text.pack(fill="x", padx=12, pady=(0, 10))
        self.family_text.configure(state="disabled")

        # Nothing has been measured yet, so neither section has anything true to
        # say. A row of six zeros would read as "we measured zero problems", which
        # is a claim this window has not earned.
        self._set_sections(show_chips=False, show_family=False)

    def _set_sections(self, show_chips: bool, show_family: bool) -> None:
        """Show or hide the two lower sections, preserving their order.

        ``pack`` always appends, so the order has to be re-established by hand —
        otherwise a second run could come back with the family clue sitting above
        the problem counts.
        """
        self.chips_section.pack_forget()
        self.family_section.pack_forget()
        if show_family:
            self.family_section.pack(fill="x")
        if show_chips:
            if show_family:
                self.chips_section.pack(fill="x", before=self.family_section)
            else:
                self.chips_section.pack(fill="x")

    def _build_log(self) -> None:
        wrap = ttk.LabelFrame(self.root, text="运行日志", padding=(8, 6, 8, 8))
        wrap.pack(fill="both", expand=True, padx=16, pady=(8, 14))
        self.log = ScrolledText(
            wrap, wrap="word", height=14, font=("Consolas", 9), state="disabled"
        )
        self.log.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ helpers

    def _default_out_dir(self) -> Path:
        base = Path.home() / "Documents"
        if not base.is_dir():
            base = Path.home()
        import time

        return base / "relaycheck报告" / time.strftime("%Y%m%d-%H%M%S")

    def _toggle_key(self) -> None:
        self.key_entry.configure(show="" if self.show_key.get() else "●")

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
        state = "disabled" if running else "normal"
        for widget in (self.start_btn, self.fetch_btn):
            widget.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.open_btn.configure(
            state="normal" if (self.last_out_dir and self.last_out_dir.is_dir()) else "disabled"
        )

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
        """Repaint the whole card, recursing so a new section cannot be forgotten.

        The chips are painted separately: their fill carries meaning (see
        ``_SEVERITY_FILL``) and must not be overwritten with the card colour.
        """
        chips = {id(chip) for chip in self.chips.values()}

        def paint(widget: tk.Misc) -> None:
            if id(widget) not in chips:
                try:
                    widget.configure(background=bg)
                except tk.TclError:
                    pass
            for child in widget.winfo_children():
                paint(child)

        paint(self.card)
        for rule in (self.chips_rule, self.family_rule):
            rule.configure(background=_shade(bg, 0.9))

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
        """The whole card as plain text — the handle tests assert on, and what a
        future 「复制结论」 button will hand over.

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

    def _show_card(
        self,
        verdict: str,
        detail: str,
        counts: dict[str, int] | None,
        family_rows: Sequence[tuple[str, str, str]] | None = None,
    ) -> None:
        fg, bg, _ = VERDICT_STYLE.get(verdict, ("#333333", "#eeeeee", ""))
        self._tint(bg)
        self.verdict_label.configure(text=verdict, foreground=fg)
        self.verdict_detail.configure(text=detail, foreground="#333333")

        # A chip is filled only when its count is non-zero: a solid red
        # ``CRITICAL 0`` on a run that found nothing reads as an alarm, which is
        # the opposite of what the card says. Filled means "this happened"; grey
        # means "this did not".
        for key, tag in _SEVERITY_CHIPS:
            count = (counts or {}).get(key, 0)
            fill_bg, fill_fg = _SEVERITY_FILL.get(key, ("#475569", "#ffffff"))
            self.chips[key].configure(
                text=f"{tag} {count}",
                background=fill_bg if count > 0 else bg,
                foreground=fill_fg if count > 0 else _SEVERITY_EMPTY_FG,
            )

        rows = list(family_rows or ())
        # Sections first, then content: the family Text sizes itself from its own
        # laid-out width, and a widget that is still unpacked has none.
        self._set_sections(bool(counts), bool(rows))
        self._fill_family(rows)
        self.root.update_idletasks()

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
        self.fetch_btn.configure(state="disabled")
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
        self.fetch_btn.configure(state="normal")
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
            fg, bg, detail = _FAIL_STYLE
            if code in (0, 1):
                # Exit 0/1 mean the audit ran; no report.json means it was killed
                # before writing. Saying "运行失败" without that nuance would hide
                # the difference between "the relay is clean" and "we stopped it".
                detail = ("检测被中断，报告没写出来。没有报告不代表中转站有问题，"
                          "也不代表没问题 —— 只代表这次没查完。")
            self._show_card("运行失败", detail, None)
            self.verdict_label.configure(foreground=fg)
            self.card.configure(background=bg)
            return

        verdict = str(report.get("verdict") or "运行结束")
        counts = report.get("severity_counts") or {}
        _, _, plain = VERDICT_STYLE.get(verdict, ("", "", "见报告。"))
        findings = report.get("findings") or []
        top = ""
        if findings:
            worst = findings[0]
            top = f"\n最严重的一条：{worst.get('id', '')}  {worst.get('title', '')}"
        self._show_card(verdict, plain + top, counts, _family_rows(report))
        self._log("")
        self._log(f"报告：{self.last_out_dir / 'report.md'}")
        self._log(f"原始：{self.last_out_dir / 'report.json'}")
        self._log(f"请求 {report.get('requests_made', '?')} 次，"
                  f"耗时 {report.get('duration_s', '?')}s")
        self.open_btn.configure(state="normal")


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
