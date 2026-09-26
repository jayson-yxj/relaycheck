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


# ------------------------------------------------------------------------- the UI

class RelayCheckApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.proc: AuditProcess | None = None
        self.last_out_dir: Path | None = None
        self._model_names: list[str] = []

        root.title(f"{APP_TITLE} {__version__}")
        root.geometry("980x800")
        root.minsize(840, 660)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

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
        ttk.Label(
            bar,
            text="注意：检测会消耗你自己的 API 额度（一般几十到上百次请求）。",
            foreground="#a1541a",
        ).pack(side="right")

    def _build_verdict(self) -> None:
        self.card = tk.Frame(self.root, bd=1, relief="solid", background="#eeeeee")
        self.card.pack(fill="x", padx=16, pady=(6, 0))
        self.verdict_label = tk.Label(
            self.card, text="还没跑过", font=("Microsoft YaHei UI", 13, "bold"),
            background="#eeeeee", anchor="w", justify="left",
        )
        self.verdict_label.pack(fill="x", padx=12, pady=(10, 2))
        self.verdict_detail = tk.Label(
            self.card, text="", background="#eeeeee", anchor="w", justify="left",
            wraplength=900,
        )
        self.verdict_detail.pack(fill="x", padx=12, pady=(0, 6))
        self.chips_label = tk.Label(
            self.card, text="", background="#eeeeee", anchor="w", justify="left",
            font=("Consolas", 10),
        )
        self.chips_label.pack(fill="x", padx=12, pady=(0, 10))

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

    def _show_card(self, verdict: str, detail: str, counts: dict[str, int] | None) -> None:
        fg, bg, _ = VERDICT_STYLE.get(verdict, ("#333333", "#eeeeee", ""))
        for widget in (self.card, self.verdict_label, self.verdict_detail, self.chips_label):
            widget.configure(background=bg)
        self.verdict_label.configure(text=verdict, foreground=fg)
        self.verdict_detail.configure(text=detail, foreground="#333333")
        if counts:
            self.chips_label.configure(
                text="   ".join(f"{tag}={counts.get(key, 0)}" for key, tag in _SEVERITY_CHIPS),
                foreground="#333333",
            )
        else:
            self.chips_label.configure(text="")
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
            self._log(line)

        if done:
            self._finish()
            return
        self.root.after(120, self._drain)

    def _finish(self) -> None:
        assert self.proc is not None
        code = self.proc.returncode
        self._set_running(False)
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
        self._show_card(verdict, plain + top, counts)
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
