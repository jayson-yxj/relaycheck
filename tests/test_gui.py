"""Tests for the desktop shell.

The GUI is supposed to be a thin shell — it builds an argv, runs the CLI in a child
process, and renders whatever ``report.json`` says. If that is all it does, there is
almost nothing worth testing: the audit logic is already pinned by the 15 end-to-end
checks in ``test_mock_relay.py``.

What *is* worth testing here are the three things the shell decides on its own, and
all three fail silently:

1. **The subscription key must never reach a command line.** Windows lets any
   process read another process's argv over WMI. Keeping the key in the environment
   is a deliberate design choice, and a choice nobody notices is broken until it is
   on a screenshot. The test drives the real ``AuditProcess.start`` with
   ``subprocess.Popen`` swapped out and inspects exactly what a sniffer would see.

2. **The windowed re-dispatch has to keep working.** ``relaycheck-gui.exe`` can act
   as the audit engine for its own child processes. A windowed PyInstaller build is
   started with ``sys.stdout is None``, so it only works because ``main`` repairs the
   stream first. That is one deleted line away from
   ``AttributeError: 'NoneType' object has no attribute 'write'``.

3. **The verdict card must not fall out of sync with the reporter.** The card maps
   the five verdict strings to colours and plain-language text. Those strings are
   read straight out of ``reporter.Report.verdict`` here, so rewording a verdict in
   the reporter fails this test instead of quietly producing an unstyled card.

Everything below skips cleanly where tkinter is missing (a headless Linux CI runner
has no ``_tkinter``) and the window-building checks additionally skip without a
display.

Run with pytest, or directly::

    python tests/test_gui.py
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "gui"))

from relaycheck.cli import _make_output_safe  # noqa: E402
from relaycheck.probes import (  # noqa: E402
    ALL_PROBES,
    DEFAULT_PROBE_NAMES,
    HEAVY_PROBE_NAMES,
)

# Same reason as in the other two test files: a legacy console code page cannot
# encode the Chinese failure messages, so an unguarded print() inside a check turns
# an assertion failure into an unreadable UnicodeEncodeError.
_make_output_safe()

try:
    import pytest
except ImportError:  # standalone run, no pytest installed
    pytest = None  # type: ignore[assignment]

try:
    import tkinter  # noqa: F401

    _TK_ERROR: Exception | None = None
except ImportError as exc:  # e.g. headless Linux without libtk8.6
    _TK_ERROR = exc

if _TK_ERROR is None:
    import relaycheck_gui as G
else:  # pragma: no cover - depends on the platform
    G = None  # type: ignore[assignment]

#: Hermetic theme file for the whole module.
#:
#: ``RelayCheckApp(root)`` without a ``theme=`` argument calls ``load_theme()``,
#: which reads ``~/.relaycheck/ui.json``. Nineteen tests here build an app that
#: way, so without this line the suite would quietly take on the colours whoever
#: ran it last happened to pick by clicking the toggle. A test that passes on a
#: machine that has never clicked it and fails on one that has is a flake wearing
#: a costume. Point the lookup at a path that cannot exist and the default wins.
_HERMETIC_THEME_DIR: str | None = None
if G is not None:
    _HERMETIC_THEME_DIR = tempfile.mkdtemp(prefix="relaycheck-gui-tests-")
    os.environ[G._THEME_ENV] = str(Path(_HERMETIC_THEME_DIR) / "ui.json")


def _skip(reason: str) -> None:
    if pytest is not None:
        pytest.skip(reason)
    print(f"  skip {reason}")


def _need_gui() -> bool:
    if G is None:
        _skip(f"tkinter unavailable: {_TK_ERROR}")
        return False
    return True


_ROOT = None
_ROOT_TRIED = False


def _tk_root():
    """One Tk root for the whole process, or None where there is no display.

    Deliberately created once and never destroyed. Creating a *second* Tcl
    interpreter after the first one has been torn down is unreliable: it works most
    of the time and then raises on some run, which turns into a test that skips
    **sometimes**. A window test that silently skips on a machine that does have a
    display is a coverage hole wearing the costume of a passing test, so the failure
    is forced to be all-or-nothing and loud instead.
    """
    global _ROOT, _ROOT_TRIED
    if _ROOT is None and not _ROOT_TRIED:
        _ROOT_TRIED = True
        try:
            import tkinter as tk

            _ROOT = tk.Tk()
        except Exception:  # noqa: BLE001 - TclError with no DISPLAY, or no Tk at all
            _ROOT = None
    return _ROOT


# ------------------------------------------------------- 1. argv / env hygiene


def test_audit_process_never_puts_the_key_in_the_command_line() -> None:
    """The one guarantee that matters when someone screenshots the window.

    Drives the real ``AuditProcess.start`` with ``subprocess.Popen`` replaced, then
    asserts on the two things an attacker on the same machine could actually read:
    the argv list and the environment block.
    """
    if not _need_gui():
        return

    secret = "sk-secret-do-not-leak-0123456789"
    audit_args = ["-u", "http://127.0.0.1:9", "--out-dir", r"C:\tmp\relaycheck"]
    seen: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            seen["cmd"] = list(cmd)
            seen["env"] = dict(kwargs.get("env") or {})
            seen["encoding"] = kwargs.get("encoding")
            seen["creationflags"] = kwargs.get("creationflags", 0)
            self.stdout = io.StringIO("")
            self.returncode = 0

        def wait(self) -> int:
            return 0

        def poll(self):
            return 0

        def terminate(self) -> None:
            pass

    real_popen = G.subprocess.Popen
    G.subprocess.Popen = _FakePopen  # type: ignore[assignment]
    try:
        proc = G.AuditProcess(audit_args, secret)
        proc.start()
        proc._thread.join(timeout=5)  # type: ignore[union-attr]
    finally:
        G.subprocess.Popen = real_popen  # type: ignore[assignment]

    cmd = seen["cmd"]
    env = seen["env"]
    assert isinstance(cmd, list) and isinstance(env, dict)

    assert secret not in cmd, f"the key reached argv: {cmd}"
    assert not any(secret in str(part) for part in cmd), f"the key reached argv: {cmd}"
    assert "--api-key" not in cmd and "-k" not in cmd, f"a key flag was passed: {cmd}"
    assert env.get("RELAYCHECK_API_KEY") == secret, "the key did not reach the environment"

    assert audit_args == cmd[-4:] or cmd[-4:] == audit_args, f"args got mangled: {cmd}"

    # Both of these are Windows-specific traps: the console code page would mangle
    # every Chinese line, and a console-subsystem child would flash a black window.
    assert seen["encoding"] == "utf-8", seen["encoding"]
    assert env.get("PYTHONIOENCODING") == "utf-8"
    assert env.get("PYTHONUNBUFFERED") == "1"


def test_gui_never_builds_a_key_flag() -> None:
    """The argv builder must not have grown a ``-k``/``--api-key`` of its own."""
    if not _need_gui():
        return

    source = inspect.getsource(G.RelayCheckApp._start)
    assert '"--api-key"' not in source and "'--api-key'" not in source
    assert '"-k"' not in source and "'-k'" not in source


def test_log_pane_scrubs_the_key() -> None:
    """The window is screenshot-able, so the log must redact the key."""
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    try:
        app = G.RelayCheckApp(root)
        app.key_var.set("sk-secret-abcdef")
        app._log("...Authorization: Bearer sk-secret-abcdef...")
        text = app.log.get("1.0", "end")
        assert "sk-secret-abcdef" not in text, repr(text[-160:])
        assert "***" in text, repr(text[-160:])
    finally:
        root.update_idletasks()


# ----------------------------------------------------- 2. the windowed re-dispatch


def test_repair_standard_streams_revives_a_nulled_stream() -> None:
    """A windowed bundle starts with ``sys.stdout is None``; print must survive it."""
    if not _need_gui():
        return

    saved = sys.stdout, sys.stderr
    sys.stdout = None  # type: ignore[assignment]
    sys.stderr = None  # type: ignore[assignment]
    try:
        G._repair_standard_streams()
        assert sys.stdout is not None, "stdout was still None after the repair"
        assert sys.stderr is not None, "stderr was still None after the repair"
        print("print survives")  # the actual failure mode, exercised for real
    finally:
        sys.stdout, sys.stderr = saved


def test_frozen_without_a_sibling_engine_falls_back_to_self_dispatch() -> None:
    """With no ``relaycheck.exe`` beside us, re-enter ourselves with --run-audit."""
    if not _need_gui():
        return

    saved_frozen = getattr(sys, "frozen", None)
    saved_exe = sys.executable
    sys.frozen = True  # type: ignore[attr-defined]
    # A directory that provably has no sibling console build.
    sys.executable = str(ROOT / "gui" / "not-a-real-build" / "relaycheck-gui.exe")
    try:
        cmd = G._child_command(["-u", "https://example.invalid"])
    finally:
        sys.executable = saved_exe
        if saved_frozen is None:
            delattr(sys, "frozen")
        else:
            sys.frozen = saved_frozen  # type: ignore[attr-defined]

    assert cmd[0] == str(ROOT / "gui" / "not-a-real-build" / "relaycheck-gui.exe"), cmd
    assert cmd[1] == "--run-audit", cmd
    assert cmd[-2:] == ["-u", "https://example.invalid"], cmd


def test_run_audit_reaches_the_real_cli() -> None:
    """``--run-audit`` must be handled before Tk is touched, and must exit properly.

    argparse signals ``--version`` and usage errors by raising ``SystemExit``, which
    is a BaseException and therefore passes straight through ``main``'s
    ``except Exception``. Pinning the codes here is what keeps the GUI's exit-code
    handling honest: 0 is "ran fine", 2 is "you typed something wrong".
    """
    if not _need_gui():
        return

    def run(argv):
        buf, real = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            try:
                code = G.main(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 0
        finally:
            sys.stdout = real
        return code, buf.getvalue()

    code, text = run(["--run-audit", "--version"])
    assert code == 0, code
    assert text.strip().startswith("relaycheck "), repr(text)

    code, text = run(["--run-audit", "--list-probes"])
    assert code == 0, code
    assert "reliability" in text, repr(text[:200])

    code, _ = run(["--run-audit", "--definitely-not-a-flag"])
    assert code == 2, f"usage errors must be EXIT_ERROR, got {code}"


# ----------------------------------------------- 3. the verdict card stays in sync


def test_verdict_table_covers_every_verdict_the_reporter_can_return() -> None:
    """Read the strings out of the reporter instead of trusting a hand-written list.

    A hardcoded copy of the five verdicts would pass even after someone reworded one
    of them, and the only symptom would be a card with no colour and no explanation.
    """
    if not _need_gui():
        return

    from relaycheck.reporter import Report

    source = inspect.getsource(Report.verdict.fget)  # type: ignore[union-attr]
    import re

    returned = set(re.findall(r'return "(检测到[^"]*|未检测到[^"]*)"', source))
    assert len(returned) >= 4, f"could not read the verdicts out of the reporter: {returned}"

    missing = returned - set(G._VERDICT_TEXT)
    assert not missing, f"the GUI has no styling for: {sorted(missing)}"
    for verdict in returned:
        fg, bg, plain = G.verdict_style(verdict)
        assert fg.startswith("#") and bg.startswith("#"), (verdict, fg, bg)
        assert len(plain) > 10, (verdict, plain)


def test_the_clean_verdict_keeps_its_caveat() -> None:
    """``CLEAN`` must never read as a clean bill of health.

    The whole tool is built on one invariant: "we did not find anything" is not the
    same claim as "there is nothing". A reassuring green card is exactly where that
    distinction gets lost, so the caveat is asserted, not left to review.
    """
    if not _need_gui():
        return

    _, _, plain = G.verdict_style("未检测到问题")
    assert "不等于" in plain, plain
    assert "家族" in plain, plain


def test_the_failure_card_does_not_imply_a_verdict() -> None:
    """A crashed run must not look like a result, in either direction."""
    if not _need_gui():
        return

    _, _, plain = G.fail_style()
    assert "不代表中转站有问题" in plain, plain
    assert "也不代表没问题" in plain, plain


def test_card_survives_an_unknown_verdict() -> None:
    """A future verdict string must not crash the window after a multi-minute run."""
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    try:
        app = G.RelayCheckApp(root)
        app._show_card("某个将来才会有的结论", "说明", {"info": 7})
        assert app.verdict_label["text"] == "某个将来才会有的结论"
        assert "INFO=7" in app._card_text()
    finally:
        root.update_idletasks()


# ----------------------------------------------------------------- window itself


def test_the_card_lists_what_each_model_said_it_was() -> None:
    """The family clue is the one result a non-expert actually takes away.

    The identity probe's per-model claims only reached the window as a finding
    *title* (「模型自称的厂商与售卖名称不一致」); the claims themselves, and the
    quote that produced them, stayed in ``report.json``. Showing them is what
    makes this tool's own caveat — 家族级线索，不能当判决书 — checkable rather than
    merely asserted: the reader sees that the "wrong" answer is stock
    "I was created by OpenAI" boilerplate and understands why it cannot carry a
    verdict.
    """
    if not _need_gui():
        return

    report = {
        "findings": [
            {
                "id": "id-100",
                "evidence": {
                    "contradictions": [
                        {
                            "model": "deepseek-chat",
                            "expected_family": "deepseek",
                            "reported_family": ["minimax"],
                            "quote": "I was developed by MiniMax.",
                        }
                    ]
                },
            }
        ],
        "results": [
            {
                "probe": "identity",
                "data": {
                    "observations": {
                        "gpt-4o": {
                            "expected_family": "openai",
                            "self_reported_families": ["openai"],
                            "confirmed_families": [],
                            "verdict": "consistent",
                        },
                        "deepseek-chat": {
                            "expected_family": "deepseek",
                            "self_reported_families": ["minimax"],
                            "confirmed_families": ["minimax"],
                            "verdict": "contradiction",
                        },
                    }
                },
            },
            # A probe that is not ``identity`` must be ignored, not misread.
            {"probe": "twins", "data": {"observations": {"x": {"verdict": "contradiction"}}}},
        ],
    }

    lines = G._family_lines(report)
    assert len(lines) == 2, lines
    # The contradiction leads, and carries the exact sentence the probe quoted —
    # read out of the finding's own evidence, never re-derived here.
    assert lines[0].startswith("  ! "), lines[0]
    assert "deepseek-chat" in lines[0], lines[0]
    assert "minimax" in lines[0], lines[0]
    assert "I was developed by MiniMax." in lines[0], lines[0]
    # A self-report that matches its sales name is reported as matching, not
    # silently dropped, and its quote is not paraded as a clue.
    assert lines[1].startswith("  = "), lines[1]
    assert "gpt-4o" in lines[1], lines[1]
    assert "I was created by OpenAI." not in lines[1], "一致的自述不该被当线索引用"

    # Alignment: Consolas covers ASCII but Tk substitutes a proportional font for
    # the CJK runs, so the 售卖 column only lines up if the (always-ASCII) model
    # name is padded to the batch width first. Two different name lengths are the
    # whole point of the check.
    assert len({line.index("售卖") for line in lines}) == 1, lines
    # And the quotes have to stack too: the family names are ASCII but the
    # punctuation around them is not, so the column is measured, not counted.
    quote_cols = {
        G._display_width(line.split("「")[0]) for line in lines if "「" in line
    }
    assert len(quote_cols) == 1, lines

    # Nothing to say must produce nothing — not an empty「家族线索：」header.
    assert G._family_lines({}) == []
    assert G._family_lines({"results": [{"probe": "twins", "data": {}}]}) == []
    # An unseen verdict degrades to "no usable self-report" rather than a blank
    # line or a crash, and a failed collection says so.
    unseen = G._family_lines(
        {"results": [{"probe": "identity", "data": {"observations": {"m": {"verdict": "brand-new"}}}}]}
    )
    assert unseen and "没拿到可用的自述" in unseen[0], unseen
    failed = G._family_lines(
        {"results": [{"probe": "identity", "data": {"observations": {"m": {"error": "boom"}}}}]}
    )
    assert failed and "自述采集失败" in failed[0], failed


def test_clean_and_info_records_are_not_called_the_most_serious_problem() -> None:
    """A clean card must not contradict itself in the next sentence."""
    if not _need_gui():
        return

    harmless = [
        {"id": "bill-200", "title": "面板可读取", "severity": "info"},
        {"id": "rel-clean", "title": "可用性正常", "severity": "clean"},
    ]
    assert G._top_finding_line(harmless) == ""

    mixed = harmless + [
        {"id": "twins-100", "title": "跨厂商输出一致", "severity": "high"}
    ]
    line = G._top_finding_line(mixed)
    assert "最严重的一条" in line
    assert "twins-100" in line and "跨厂商输出一致" in line
    assert "rel-clean" not in line and "bill-200" not in line


def test_switching_cards_clears_the_previous_runs_family_lines() -> None:
    """The card is one reused widget, so a stale clue must not survive into it.

    Run twice in a row, stop the second run, and without this the *first* relay's
    self-reports would still be sitting under 「运行失败」 — attributing one
    endpoint's model identities to another.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    try:
        app = G.RelayCheckApp(root)
        app._show_card(
            "检测到中等问题", "说明", {"medium": 1}, [("  ! m  售卖 a，自称 ", "b", "")]
        )
        assert "自称 b" in app._card_text(), app._card_text()
        assert "家族线索" in app._card_text()

        # Every path with no family data must clear it — including 「正在检测…」
        # and 「运行失败」, which never call _family_rows at all.
        app._show_card("运行失败", "说明", None)
        assert app.family_section.winfo_manager() == "", "失败卡不该留着上一次的家族线索"
        assert "家族线索" not in app._card_text(), app._card_text()
    finally:
        root.update_idletasks()


# ---------------------------------------------- 4. the card's sections and colours


def _two_models_that_both_claim_openai() -> dict:
    """Two models sold as different vendors that both answer "OpenAI".

    That is the shape a real run keeps producing: distillation residue leaves the
    same stock sentence on models with nothing else in common, and making that
    visible at a glance — without reading every line — is the entire reason the
    self-reported vendor is tinted.
    """
    return {
        "results": [
            {
                "probe": "identity",
                "data": {
                    "observations": {
                        "gemini-1.5-pro": {
                            "expected_family": "google",
                            "self_reported_families": ["openai"],
                            "confirmed_families": ["openai"],
                            "verdict": "contradiction",
                        },
                        "deepseek-chat": {
                            "expected_family": "deepseek",
                            "self_reported_families": ["openai"],
                            "confirmed_families": ["openai"],
                            "verdict": "contradiction",
                        },
                    }
                },
            }
        ]
    }


def test_a_card_that_measured_nothing_shows_no_sections() -> None:
    """A row of six grey zeros would read as "we measured zero problems".

    On the failure card that is a claim the run never earned — it did not measure
    anything at all — so the sections are hidden rather than zeroed.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    try:
        app = G.RelayCheckApp(root)
        assert app.chips_section.winfo_manager() == "", "启动时不该有问题数量区"
        assert app.family_section.winfo_manager() == "", "启动时不该有家族线索区"

        app._show_card("运行失败", "说明", None)
        assert app.chips_section.winfo_manager() == "", "没查成就不该有问题数量"
        assert app.family_section.winfo_manager() == ""
        assert "CRITICAL" not in app._card_text(), app._card_text()
    finally:
        root.update_idletasks()


def test_the_sections_come_back_in_the_same_order_after_being_hidden() -> None:
    """``pack`` appends, so re-showing a section has to restore its position.

    Without the explicit ``before=`` in ``_set_sections``, the second run's card
    comes back with the family clue sitting above the problem counts — a layout
    that only appears on the *second* run, which is exactly the kind of thing a
    screenshot review of a fresh window never catches.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    try:
        app = G.RelayCheckApp(root)
        rows = G._family_rows(_two_models_that_both_claim_openai())
        app._show_card("检测到中等问题", "说明", {"medium": 1}, rows)
        first = [id(w) for w in app.card.pack_slaves()]
        assert app.card.pack_slaves().index(app.chips_section) < app.card.pack_slaves().index(
            app.family_section
        ), "问题数量必须排在家族线索前面"

        # Hide both, then bring them back — the order must survive the round trip.
        app._show_card("运行失败", "说明", None)
        app._show_card("检测到中等问题", "说明", {"medium": 1}, rows)
        assert [id(w) for w in app.card.pack_slaves()] == first, "重排后顺序变了"

        # And a card with only families, or only counts, still lands in order.
        app._show_card("正在检测…", "说明", {"medium": 1}, [])
        assert app.chips_section.winfo_manager() and not app.family_section.winfo_manager()
        app._show_card("正在检测…", "说明", None, rows)
        assert app.family_section.winfo_manager() and not app.chips_section.winfo_manager()
    finally:
        root.update_idletasks()


def test_a_severity_chip_is_only_filled_when_it_actually_happened() -> None:
    """A filled red ``CRITICAL 0`` is an alarm, on the card where it can least be one.

    The chip has to distinguish "this happened" from "this did not". Colouring a
    zero says the opposite of what its own number says, and it does it on the one
    card where the reader most needs to be told that nothing was found.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    try:
        app = G.RelayCheckApp(root)
        app._show_card("未检测到问题", "说明", {"info": 2, "clean": 7})
        card_bg = G.verdict_style("未检测到问题")[1]

        # Semantic colour belongs to the conclusion, not to every piece of
        # evidence underneath it. A full green card reads like a certificate of
        # innocence, which this family-level audit explicitly cannot issue.
        assert str(app.card_header["background"]) == card_bg
        assert str(app.card["background"]) == G.PALETTE["surface"]

        for key in ("critical", "high", "medium", "low"):
            chip = app.chips[key]
            assert str(chip["background"]) == G.PALETTE["surface"], f"{key} 计数为 0，不该上色"
            assert str(chip["foreground"]) == G.PALETTE["severity_empty_fg"], key
            assert chip["text"].endswith(" 0"), chip["text"]
        for key in ("info", "clean"):
            chip = app.chips[key]
            fill_bg, fill_fg = G.severity_fill(key)
            assert str(chip["background"]) == fill_bg, key
            assert str(chip["foreground"]) == fill_fg, key
            assert str(chip["background"]) != card_bg, key
        assert "INFO=2" in app._card_text() and "CLEAN=7" in app._card_text()

        # Evidence sections are neutral, so their dividers use the neutral border
        # rather than extending the verdict tint through the entire card.
        assert str(app.chips_rule["background"]) == G.PALETTE["border_soft"]
        assert str(app.chips_rule["background"]) != card_bg
    finally:
        root.update_idletasks()


def test_the_family_block_tints_the_claim_and_not_the_name_being_checked() -> None:
    """Colour says "these two lines are the same claim", never "this model is fake".

    Tinting the whole line would paint 「售卖 google」 — the name being *checked* — in
    the colour of the claim being *made*, which inverts what the block means. The
    tint must cover exactly the self-reported vendor and nothing else.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    try:
        report = _two_models_that_both_claim_openai()
        rows = G._family_rows(report)
        app = G.RelayCheckApp(root)
        app._show_card("检测到中等问题", "说明", {"medium": 1}, rows)

        widget = app.family_text
        tinted: list[tuple[str, str]] = []
        for tag in widget.tag_names():
            if not str(tag).startswith("fam:"):
                continue
            ranges = widget.tag_ranges(tag)
            for start, end in zip(ranges[::2], ranges[1::2]):
                tinted.append((widget.get(start, end), str(widget.tag_cget(tag, "foreground"))))
        assert tinted, "家族线索一句都没上色"
        assert {text for text, _ in tinted} == {"openai"}, f"上色的片段不对：{tinted}"
        assert len({colour for _, colour in tinted}) == 1, "同一个自称厂商必须是同一个颜色"

        # The widget and the plain-text renderer must not drift: one datum, two
        # renderings. 售卖 is on the line, and it is not inside any tinted span.
        assert widget.get("1.0", "end-1c").split("\n") == G._family_lines(report)
        assert all("售卖" not in text for text, _ in tinted), tinted
    finally:
        root.update_idletasks()


def test_a_vendors_colour_is_the_same_on_every_run() -> None:
    """``hash()`` is salted per process, so a colour picked that way would change
    on every launch and "same vendor, same colour" would quietly become a lie.

    Checked at the source rather than by observation, because the failure is
    invisible from inside a single process.
    """
    if not _need_gui():
        return
    assert "hash(" not in inspect.getsource(G.family_color), (
        "厂商颜色不能用 hash() 决定：它按进程加盐，同一份报告每次跑颜色都不一样"
    )
    assert G.family_color("openai") == G.family_color("openai")
    assert G.family_color("OpenAI") == G.family_color("openai"), "大小写不该换颜色"
    assert G.family_color("") is None, "没有自述就没有要上色的东西"
    assert G.family_color("   ") is None

    unknown = G.family_color("some-vendor-from-2030")
    assert unknown in G.PALETTE["family_fallback"], unknown
    assert unknown == G.family_color("some-vendor-from-2030")
    # Two different unknown vendors should not collapse onto one slot every time.
    spread = {G.family_color(f"vendor-{i}") for i in range(12)}
    assert len(spread) > 1, "未知厂商全都撞到同一个颜色"
    assert all(colour in G.PALETTE["family_fallback"] for colour in spread)


def _luma(colour: str) -> float:
    """WCAG relative luminance, so two colours can be compared for lightness."""
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    """WCAG relative-luminance ratio.

    A local copy on purpose: the tests must not reach for the contrast scripts that
    live outside the repo, or CI would depend on my scratch directory.
    """
    hi, lo = sorted((_luma(a), _luma(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_every_theme_answers_every_role_the_window_asks_for() -> None:
    """A theme that is missing a role used to fail three frames deep inside a widget
    constructor, where the traceback names a tkinter option and never the colour.

    ``relaycheck_gui`` now refuses to import a short theme at all. This pins the
    shape from the outside so a half-filled third theme cannot be added quietly.
    """
    if not _need_gui():
        return
    light = G.THEMES["light"]
    for name, theme in G.THEMES.items():
        missing = [role for role in G._CHROME_ROLES if role not in theme]
        assert not missing, f"{name} 少了角色: {missing}"
        assert set(theme) == set(light), (
            f"{name} 的 key 跟 light 对不上: "
            f"多 {sorted(set(theme) - set(light))} 少 {sorted(set(light) - set(theme))}"
        )
        for role in G._CHROME_ROLES:
            value = theme[role]
            assert re.fullmatch(r"#[0-9a-f]{6}", value), f"{name}.{role} = {value!r}"


def test_the_progress_bar_is_not_painted_in_a_semantic_colour() -> None:
    """The bar only ever means "still running".

    Borrow a semantic colour and a perfectly healthy run reads as a failure — which
    is exactly what a red bar under 正在检测… says to the user. So the fill gets its
    own role, and it has to be both off the semantic palette and clearly visible
    against the slot it fills.
    """
    if not _need_gui():
        return
    for name, theme in G.THEMES.items():
        fill = theme["progress_fill"]
        semantic = {theme["danger"], theme["danger_soft"], theme["spend_warn"]}
        semantic |= {bg for bg, _fg in theme["severity_fill"].values()}
        assert fill not in semantic, f"{name}: 进度条用了语义色 {fill}"
        claimed = _contrast(fill, theme["track"])
        assert claimed >= 4.5, (
            f"{name}: 进度条对进度槽只有 {claimed:.2f}:1，看不出来在动"
        )


def test_window_builds_with_the_expected_initial_state() -> None:
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    try:
        app = G.RelayCheckApp(root)
        root.update_idletasks()
        assert str(app.start_btn["state"]) == "normal"
        assert str(app.stop_btn["state"]) == "disabled"
        assert Path(app.outdir_var.get()).is_absolute(), app.outdir_var.get()
        # grid_info() is empty exactly when the row has been grid_remove()d, and unlike
        # winfo_ismapped() it does not depend on whether the root is actually mapped —
        # the old assertion was true no matter what the widget did.
        assert app.model_list_frame.grid_info() == {}, "the model list should start hidden"
        assert app.model_list.size() == 0
        assert "开始检测" in app.log.get("1.0", "end")

        assert app._collect_models() == ""
        app.models_var.set("gpt-4o, deepseek-chat")
        assert app._collect_models() == "gpt-4o, deepseek-chat"
    finally:
        root.update_idletasks()


def test_gui_version_matches_the_package() -> None:
    if not _need_gui():
        return
    from relaycheck import __version__

    assert G.__version__ == __version__


# ------------------------------------------------------------- progress & chrome


def _walk(widget):
    """Every descendant of ``widget``, depth first."""
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def test_the_progress_bar_only_counts_what_the_cli_actually_reported() -> None:
    """The bar is the only thing moving during a four-minute dead-relay run.

    It is also the easiest widget in the window to make dishonest: it has no idea
    what the child process is doing, it only reads the child's own stdout. So the
    two failure modes worth pinning are that it counts a line that is not a probe,
    and that it invents progress it never saw.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    try:
        app = G.RelayCheckApp(root)
        # Exactly the shapes cli.py prints, plus the two intra-probe lines that
        # must change nothing at all.
        for line in (
            "探针: reliability, echo, billing-panel",
            "  → reliability …",
            "      · gpt-4o 第 1/2 次探测中…",
            "    √ reliability: info (2 请求 / 0.0s)",
            "  → echo …",
            "    √ echo: medium (4 请求 / 0.4s)",
        ):
            app._note_progress(line)

        assert app._probes_total == 3, app._probes_total
        assert app._probes_done == 2, app._probes_done
        assert float(app.progress["maximum"]) == 3.0
        assert float(app.progress["value"]) == 2.0
        assert app.status_var.get() == "已完成 2/3 项", app.status_var.get()

        # Anything unrecognised is not evidence of progress.
        for junk in ("", "   ", "搞不懂的一行", "√", "→"):
            app._note_progress(junk)
        assert app._probes_done == 2, app._probes_done
        assert app.status_var.get() == "已完成 2/3 项"
    finally:
        root.update_idletasks()


def test_a_run_that_was_stopped_does_not_get_a_full_progress_bar() -> None:
    """Two-thirds of a checklist is not a finished checklist.

    A full bar after a stop would be this window asserting something the child
    never said — the same mistake as reporting CLEAN for a probe that never ran.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    from types import SimpleNamespace

    try:
        app = G.RelayCheckApp(root)
        app._probes_total = 8
        app._probes_done = 3
        app._set_progress(app._probes_done, app._probes_total)
        app.proc = SimpleNamespace(returncode=1, running=False)
        app._finish()

        assert float(app.progress["value"]) == 3.0, app.progress["value"]
        assert float(app.progress["maximum"]) == 8.0
        assert "3/8" in app.status_var.get(), app.status_var.get()

        # And a stray over-count can never push the bar past its own end.
        app._set_progress(99, 8)
        assert float(app.progress["value"]) == 8.0
    finally:
        root.update_idletasks()


def test_the_window_actually_has_an_icon_and_it_is_the_generated_one() -> None:
    """Asserts the load, not the file — a missing icon is the bug this replaced.

    Before this, ``grep -i "icon|\\.ico"`` over ``gui/`` returned nothing at all: the
    exe shipped PyInstaller's default mark and the window showed Tk's feather. A
    test that only checked "the .ico exists" would go green again the moment the
    wiring broke, which is exactly how it was broken the first time.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return

    png = G._icon_path("png")
    assert png is not None and png.is_file(), f"窗口图标 png 找不到：{png}"
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "窗口图标不是 PNG"

    # Parsed by hand instead of with Pillow: this suite has to keep running on a CI
    # box that has nothing installed but requests and pytest.
    import struct

    ico = png.with_suffix(".ico")
    assert ico.is_file(), f"exe 图标 ico 找不到：{ico}"
    blob = ico.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", blob, 0)
    assert (reserved, kind) == (0, 1), f"不是 ICO 文件头：{(reserved, kind)}"
    sizes = sorted(blob[6 + 16 * i] or 256 for i in range(count))
    assert sizes == [16, 24, 32, 48, 64, 128, 256], (
        f"ico 里缺尺寸，16px 会由 Windows 从大图缩出来而不是取预渲染帧：{sizes}"
    )

    try:
        app = G.RelayCheckApp(root)
        assert app._icon_image is not None, (
            f"图标文件在，但 iconphoto 没生效 —— 窗口会退回 Tk 默认羽毛：{app._icon_error}"
        )
        assert app._icon_image.width() == 256, app._icon_image.width()
    finally:
        root.update_idletasks()


def test_the_spend_warning_is_bold_and_off_the_button_row() -> None:
    """The one line on screen that costs money used to be the easiest to ignore.

    It sat at the far right of the button row at body weight, a whole window away
    from the button it was warning about. Prominence *is* the fix, so prominence
    is what gets asserted.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    import tkinter as tk

    # A Toplevel rather than the shared root: every check in this file packs its app
    # onto the same long-lived root, so walking ``app.root`` would find one warning
    # label per test that ran before this one and "exactly once" would depend on
    # test order.
    top = tk.Toplevel(root)
    try:
        app = G.RelayCheckApp(top)
        hits = []
        for widget in _walk(top):
            try:
                if "API 额度" in str(widget.cget("text")):
                    hits.append(widget)
            except Exception:  # noqa: BLE001 - widget has no -text option
                continue
        assert len(hits) == 1, f"花钱提醒应恰好出现一次，实际 {len(hits)} 次"
        warn = hits[0]
        assert "bold" in str(warn.cget("font")), warn.cget("font")
        assert str(warn.cget("foreground")) == "#a1541a", warn.cget("foreground")
        # Checked structurally rather than by comparing against ``start_btn.master``,
        # because the claim is about the row, not about one instance: a TButton in
        # the warning's own frame means it is back in the button row.
        siblings = [w.winfo_class() for w in warn.master.winfo_children()]
        assert "TButton" not in siblings, (
            f"花钱提醒又回到了按钮行 —— 它得跟它提醒的那个按钮在一个视觉块里：{siblings}"
        )
    finally:
        top.update_idletasks()
        top.destroy()


def test_the_build_script_survives_a_chinese_locale_windows_powershell() -> None:
    """A BOM-less .ps1 is decoded as ANSI by Windows PowerShell 5.1, which eats lines.

    This is not a style rule. On a zh-CN box cp936 has *lead* bytes, so a CJK
    character at the end of a comment can pair with the following LF and pull the
    next source line into the comment. Six lines of ``gui/build.ps1`` were being
    swallowed, and one of them was ``$Probe = '...'`` — so ``build.ps1 -Python``
    failed with "Argument expected for the -c option" and then claimed no usable
    interpreter existed, for an interpreter that worked when typed by hand.

    CI cannot catch this: the runner is en-US, and cp1252 has no lead bytes, so the
    same file parses cleanly there. Only a BOM makes both editions read UTF-8.
    """
    # Deliberately not guarded by _need_gui(): this reads a file, so it must keep
    # running — and keep guarding — on a box with no tkinter at all.
    script = ROOT / "gui" / "build.ps1"
    assert script.is_file(), f"找不到构建脚本：{script}"
    raw = script.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", (
        "gui/build.ps1 没有 UTF-8 BOM —— Windows PowerShell 5.1 会按 ANSI(cp936) "
        "解码，中文注释会连行吞掉后面的代码"
    )
    # A BOM on a file that is not valid UTF-8 would be worse than no BOM at all.
    raw[3:].decode("utf-8")


def test_the_sdist_carries_every_file_the_desktop_build_reads() -> None:
    """``gui/`` rides along in the sdist, so anything the spec reads has to ride too.

    0.1.4 shipped an sdist whose ``gui/`` had no ``.ico`` and no ``.png``, because
    MANIFEST.in only listed ``*.py *.ps1 *.spec *.md``. That is not a cosmetic loss:
    ``relaycheck_gui.spec`` hands both paths to PyInstaller, and a missing icon makes
    it raise ``FileNotFoundError: Icon input file ... not found`` — at the very end,
    after the expensive analysis. Nobody with only that sdist could build the exe.

    This test is about the *manifest*, not about the files: the icons exist either way.
    """
    # Not guarded by _need_gui(): it reads text, so it must keep guarding everywhere.
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    line = next(
        (
            ln
            for ln in manifest.splitlines()
            if ln.strip().startswith("recursive-include gui")
        ),
        "",
    )
    assert line, "MANIFEST.in 里没有 recursive-include gui —— gui/ 根本不会进 sdist"
    patterns = set(line.split()[2:])
    spec = (ROOT / "gui" / "relaycheck_gui.spec").read_text(encoding="utf-8")
    for name in ("relaycheck.ico", "relaycheck.png"):
        asset = ROOT / "gui" / name
        assert asset.is_file(), f"缺文件：{asset}"
        # If the spec stops referencing it, this assertion is guarding nothing and
        # should be deleted along with the reference.
        assert name in spec, f"spec 已经不引用 gui/{name} 了，这条断言该一起删掉"
        assert f"*{asset.suffix}" in patterns, (
            f"MANIFEST.in 的 `recursive-include gui` 没覆盖 *{asset.suffix}，"
            f"sdist 里会缺 gui/{name}（构建 exe 会直接失败）"
        )


# --------------------------------------------- disclosure, handover, and panes


def test_the_advanced_panel_starts_closed_and_keeps_its_settings() -> None:
    """Hiding three settings is only honest if they keep the values they had.

    The panel holds the run's cost knobs: probe strength, how many models, the
    per-request timeout, the request budget. If closing it reset any of them, the
    disclosure would be quietly rewriting the thing it is hiding — and the window
    would spend someone's quota on settings they never chose.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    try:
        app = G.RelayCheckApp(root)
        assert app.adv_panel.grid_info() == {}, "高级面板应该默认收起"
        assert str(app.adv_toggle["text"]) == G._ADV_CLOSED, app.adv_toggle["text"]
        assert app.strength_var.get() == "default", app.strength_var.get()

        app.strength_var.set("all")
        app.maxmodels_var.set("9")
        app._toggle_advanced()
        assert app.adv_panel.grid_info() != {}, "点开之后面板应该露出来"
        assert str(app.adv_toggle["text"]) == G._ADV_OPEN, app.adv_toggle["text"]
        assert (app.strength_var.get(), app.maxmodels_var.get()) == ("all", "9")

        app._toggle_advanced()
        assert app.adv_panel.grid_info() == {}, "再点一次应该收回去"
        assert str(app.adv_toggle["text"]) == G._ADV_CLOSED, app.adv_toggle["text"]
        assert (app.strength_var.get(), app.maxmodels_var.get()) == ("all", "9"), (
            "收起高级面板不能把里面的设置改回默认值"
        )
    finally:
        root.update_idletasks()


def test_the_expensive_probe_is_not_in_the_default_set() -> None:
    """The default set is what runs on a first look at a relay nobody has measured.

    ``context`` sends prompts of thousands of tokens up an ascending ladder, so one
    model can cost more than the other ten probes combined — and a first exploratory
    audit against an unknown relay is the worst possible moment to spend a stranger's
    balance on that. It is also the probe most likely to be "helpfully" promoted into
    the defaults, because silent head truncation is exactly the substitution this
    tool exists to catch. This test is the brake on that: thorough is something the
    user opts into, not something we do to their card.

    Deliberately not gated on a display — the invariant is in the registry, and a
    headless runner should still refuse to let the default set drift.
    """
    assert "context" in HEAVY_PROBE_NAMES, HEAVY_PROBE_NAMES
    assert "context" not in DEFAULT_PROBE_NAMES, (
        "context 必须保持 opt-in：默认集跑在最不该花使用者钱的那一刻"
    )
    # The two sets have to stay disjoint, or the window's "8 项" and "11 项" would be
    # counting the same probe twice and be wrong in a subtler way.
    assert not (set(DEFAULT_PROBE_NAMES) & set(HEAVY_PROBE_NAMES)), DEFAULT_PROBE_NAMES
    assert len(DEFAULT_PROBE_NAMES) + len(HEAVY_PROBE_NAMES) == len(ALL_PROBES)


def test_the_strength_labels_name_the_real_probe_counts() -> None:
    """The window a user reads *before* spending money must not misstate the cost.

    Both radio labels name a number and neither is computed from the registry, so
    registering a ninth default probe would leave the window still advertising eight.
    The "all" label matters more: it is the only route to the context probe, so if it
    understates what it buys, the cost hint sitting next to it is describing a
    different set than the one that will actually run.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    try:
        app = G.RelayCheckApp(root)
        app._toggle_advanced()
        root.update_idletasks()

        options: dict[str, str] = {}
        texts: list[str] = []
        for node in _walk(app.adv_panel):
            try:
                texts.append(str(node.cget("text")))
            except Exception:
                pass
            # Only the radios. ttk.Spinbox also answers ``-value`` (with an empty
            # string) and every ttk widget with a textvariable reports the Tcl
            # variable's name as its ``-text``, so an unfiltered sweep picks up
            # ``{'': 'PY_VAR5'}`` and the assertion below stops meaning anything.
            if node.winfo_class() != "TRadiobutton":
                continue
            options[str(node.cget("value"))] = str(node.cget("text"))

        assert set(options) == {"default", "all"}, options
        assert str(len(DEFAULT_PROBE_NAMES)) in options["default"], options["default"]
        assert str(len(ALL_PROBES)) in options["all"], options["all"]
        # The expensive option has to admit that it is the expensive option...
        assert "贵" in options["all"], options["all"]
        # ...and say what the extra money buys. "更贵" on its own is just a scare.
        assert any("上万 token" in text for text in texts), texts
    finally:
        root.update_idletasks()


def test_the_handover_buttons_only_appear_once_there_is_a_conclusion() -> None:
    """「复制结论」 on the 正在检测… card would copy a sentence that says nothing.

    So it is not always-on chrome. It shows on a card that carries a result, and
    that includes the failure card: a run that broke is exactly the run whose
    output someone needs to paste into a bug report.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    try:
        app = G.RelayCheckApp(root)
        assert app.action_section.winfo_manager() == "", "启动时不该有交付按钮"

        app._show_card("正在检测…", "已启动，输出会实时显示在下面。", None)
        assert app.action_section.winfo_manager() == "", "还没出结果就没有结论可复制"

        app._show_card("运行失败", "退出码 2。", None, actions=True)
        assert app.action_section.winfo_manager() == "pack", "失败卡也要能复制结论去报 bug"
        assert app.card.pack_slaves()[-1] is app.action_section, "交付按钮应该落在卡片最后一行"
    finally:
        root.update_idletasks()


def test_the_copy_button_never_hands_over_the_api_key() -> None:
    """The one control here that writes to a shared, persistent place.

    Every other path keeps the key inside the process. A clipboard is readable by
    any program on the machine and outlives the window, so the text this button
    produces is the one place the key must never reach. The footer it does add is
    asserted too: a copied conclusion with no idea which station produced it is not
    something anyone can act on.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    secret = "sk-do-not-copy-me-0123456789"
    try:
        app = G.RelayCheckApp(root)
        app.key_var.set(secret)
        app.url_var.set("https://api.example.com")
        app.last_out_dir = None
        app._show_card("未检测到问题", "说明", {"info": 2}, None, actions=True)
        app._copy_card()

        try:
            text = root.clipboard_get()
        except Exception as exc:  # noqa: BLE001 - a bare CI box has no clipboard owner
            _skip(f"clipboard unavailable: {exc}")
            return

        assert secret not in text, "API Key 进了剪贴板"
        assert "未检测到问题" in text, text
        assert "https://api.example.com" in text, f"复制出来的结论没说是哪个站：{text}"
        assert G.APP_TITLE in text, text
        assert str(app.copy_hint["text"]) == "已复制到剪贴板", "按了没反应会被当成按坏"
    finally:
        root.update_idletasks()


def test_both_open_the_report_buttons_agree_on_whether_there_is_a_report() -> None:
    """Two buttons, one report directory. Disagreeing is the one impossible state.

    The toolbar button is the entry point before a run, the one on the card is the
    entry point while reading a result. They read the same directory, so a state
    where one is clickable and the other is grey is a state that cannot be true —
    and a directory deleted between runs is not openable either.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    try:
        app = G.RelayCheckApp(root)
        for out_dir, expected in (
            (None, "disabled"),
            (ROOT, "normal"),
            (ROOT / "_no_such_report_dir_here", "disabled"),
        ):
            app.last_out_dir = out_dir
            app._set_running(False)
            assert str(app.open_btn["state"]) == expected, (out_dir, app.open_btn["state"])
            assert str(app.card_open_btn["state"]) == expected, (
                out_dir,
                app.card_open_btn["state"],
            )
    finally:
        root.update_idletasks()


def test_the_log_pane_is_draggable_and_says_so() -> None:
    """Under this theme ttk paints the sash the same colour as the window around it.

    So the divider ships invisible: it works, and nobody finds out it is there. The
    hairline at the top of the log pane is therefore load-bearing rather than
    decoration, and the second pane is asserted because a PanedWindow holding one
    pane is a Frame that costs more.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    try:
        app = G.RelayCheckApp(root)
        assert len(app.paned.panes()) == 2, app.paned.panes()
        assert app.sash_hint.winfo_manager() == "pack", "分隔线没显示，日志可拖这件事就没人知道"
        assert str(app.sash_hint["background"]) == G.PALETTE["sash_hint"], app.sash_hint["background"]
        assert app.sash_hint.master is app.log_wrap.master, "分隔线得跟日志在同一格里"
        assert G.PALETTE["sash_hint"] != "#f0f0f0", "这跟主题底色一样，等于没画"
    finally:
        root.update_idletasks()


def test_the_divider_is_placed_after_the_form_and_not_on_top_of_it() -> None:
    """``sashpos`` before the window is mapped is accepted and then thrown away.

    That is how the first version of this shipped: the sash stayed at 0, the top
    pane — the whole form, 开始检测 included — collapsed to nothing, and the log
    took the entire window. No exception, no warning, just a window with its
    controls missing. The placement has to wait for the first real layout.
    """
    if not _need_gui():
        return
    root = _tk_root()
    if root is None:
        _skip("no display available")
        return
    import tkinter as tk

    # A Toplevel rather than the shared root: this one needs to actually get mapped,
    # and mapping the long-lived root would map every app stacked on it.
    top = tk.Toplevel(root)
    try:
        app = G.RelayCheckApp(top)
        # Reproduce the original failure exactly: the form is measured, and the
        # divider is placed from that measurement, all while the paned window is
        # still 1px tall. ttk accepts the call, cannot honour it, and settles on 0
        # for good — it does not re-derive the position when the window is mapped.
        top.update_idletasks()
        app._reset_sash()
        top.update()
        if app.paned.winfo_height() <= 1:
            _skip("the paned window never got a size")
            return
        need = app.top.winfo_reqheight()
        assert app.paned.sashpos(0) >= need, (
            f"分界线压在表单上：sash={app.paned.sashpos(0)}，表单要 {need}"
        )
        assert app.start_btn.winfo_ismapped(), "开始检测按钮被分界线挤出可视区了"
        assert app.log.winfo_ismapped(), "日志区没被布局"
    finally:
        top.update_idletasks()
        top.destroy()


# ------------------------------------------------- 第四刀：主题开关 / 画布渐变


@contextlib.contextmanager
def _isolated_window(theme: str = "light"):
    """一个用完就扔的窗口，主题文件指向临时目录。

    测试绝不能碰 ``~/.relaycheck/ui.json``：跑一遍测试就把开发者自己那个窗口下次
    开成什么颜色改掉了，是最讨厌的一种副作用。环境变量是 ``relaycheck_gui`` 唯一
    的覆盖口，存在 / 还原都走它。
    """
    _need_gui()
    import tkinter as tk

    root = _tk_root()
    if root is None:
        _skip("no display available")
    with tempfile.TemporaryDirectory() as tmp:
        previous = os.environ.get(G._THEME_ENV)
        os.environ[G._THEME_ENV] = str(Path(tmp) / "ui.json")
        top = tk.Toplevel(root)
        try:
            yield top, G.RelayCheckApp(top, theme=theme), Path(tmp)
        finally:
            top.update_idletasks()
            top.destroy()
            if previous is None:
                os.environ.pop(G._THEME_ENV, None)
            else:
                os.environ[G._THEME_ENV] = previous


def _chrome_colours(app) -> dict[str, str]:
    """用户能看出主题变了的那几个面。"""
    widgets = {
        "top": app.top,
        "content": app.content,
        "theme_btn": app.theme_btn,
        "start_btn": app.start_btn,
        "stop_btn": app.stop_btn,
        "adv_toggle": app.adv_toggle,
        "status_dot": app.status_dot,
        "card": app.card,
        "card_accent": app.card_accent,
        "verdict_label": app.verdict_label,
        "sash_hint": app.sash_hint,
        "log_wrap": app.log_wrap,
        "log": app.log,
        "progress": app.progress,
    }
    colours: dict[str, str] = {}
    for name, widget in widgets.items():
        for option in ("background", "foreground"):
            try:
                value = widget.cget(option)
            except Exception:  # noqa: BLE001 - 不是每个控件两个选项都有
                continue
            if value:
                colours[f"{name}.{option}"] = str(value).lower()
    return colours


def test_switching_theme_keeps_what_the_user_typed_and_what_was_measured() -> None:
    """换主题是重建整棵控件树，所以它必须自己把用户的东西搬过去。

    搬丢任何一样，用户看到的就是「点了一下深色，填的地址没了」——而这个功能本来
    只是想让晚上看着舒服点。
    """
    with _isolated_window("light") as (top, app, _tmp):
        out_dir = str(Path(tempfile.gettempdir()) / "relaycheck-out")
        app.url_var.set("https://relay.example.test/v1")
        app.key_var.set("sk-not-a-real-key-0123456789")
        app.models_var.set("deepseek-v4-flash, qwen-max")
        app.outdir_var.set(out_dir)
        app._log("第一行")
        app._log("第二行")
        app._set_progress(3, 8)
        app._show_card(
            "未检测到问题",
            "本次没有发现达到阈值的证据。",
            {"critical": 0, "info": 2},
            family_rows=[("deepseek-v4-flash", "deepseek", "一致")],
            actions=True,
        )

        app._apply_theme("dark")
        top.update()

        assert G.PALETTE is G.DARK
        assert app.url_var.get() == "https://relay.example.test/v1"
        assert app.key_var.get() == "sk-not-a-real-key-0123456789"
        assert app.models_var.get() == "deepseek-v4-flash, qwen-max"
        assert app.outdir_var.get() == out_dir
        assert "第一行" in app.log.get("1.0", "end-1c")
        assert "第二行" in app.log.get("1.0", "end-1c")
        assert app.progress.cget("maximum") == 8.0
        assert app.progress.cget("value") == 3.0
        assert app.verdict_label.cget("text") == "未检测到问题"
        assert app.card.winfo_ismapped(), "切主题之后结论卡片没了"


def test_switching_back_and_forth_reproduces_the_same_colours() -> None:
    """浅色 -> 深色 -> 浅色 必须落到同一套颜色上。

    这正是主题开关选择「重建控件树」而不是「遍历控件刷一遍色」的理由：刷色要知道
    每一个派生颜色（``_shade``、``_tint`` 都算），漏一个的表现恰好是「大部分变了，
    这一块没变」。重建漏不了，这条测试就是那句断言的机器版本。
    """
    with _isolated_window("light") as (top, app, _tmp):
        top.geometry("980x800")

        def sample() -> dict[str, str]:
            # 画布渐变按绝对 y 取色，所以要先把窗口摆到最终几何再采样，否则两次
            # 采到的位置不同，比出来的是布局抖动而不是配色。
            top.update()
            app._paint_ramp()
            top.update_idletasks()
            return _chrome_colours(app)

        before = sample()
        app._apply_theme("dark")
        middle = sample()
        app._apply_theme("light")
        after = sample()

    assert G.PALETTE is G.LIGHT
    assert middle != before, "切到深色之后一个颜色都没变，主题开关是摆设"
    drift = {k: (before[k], after[k]) for k in before if before.get(k) != after.get(k)}
    assert not drift, f"绕一圈没回到同一套颜色: {drift}"


def test_the_theme_toggle_shows_the_theme_you_will_get() -> None:
    """按钮上写的必须是「按下去会变成什么」，不是「现在是什么」。

    一个写着「深色」的按钮在深色窗口里，两件事都说得通，用户只能点一下试试。
    """
    with _isolated_window("light") as (_top, app, _tmp):
        assert app.theme_btn.cget("text") == "深色"
        app._toggle_theme()
        assert G.PALETTE is G.DARK
        assert app.theme_btn.cget("text") == "浅色"
        app._toggle_theme()
        assert G.PALETTE is G.LIGHT
        assert app.theme_btn.cget("text") == "深色"


def test_every_test_that_touches_a_widget_confirms_there_is_a_display_first() -> None:
    """碰了 tk 控件的测试，必须先自己确认「这台机器上有显示器」。

    这条是拿真实事故换来的。CI 上 ubuntu 三条腿红、Windows 和 macOS 绿，报的是::

        RuntimeError: Too early to create image: no default root window

    原因很窄：ubuntu 跑器 **装得上 tkinter，但没有 DISPLAY**。于是 ``tk.Tk()`` 失败、
    ``_tk_root()`` 返回 None —— 而 ``_need_gui()`` 只检查「tkinter 能不能 import」，它
    照样通过。任何 ``tk.PhotoImage`` / ``tk.Toplevel`` / ``RelayCheckApp`` 接着就炸。

    这类错误在本机永远看不见（本机有显示器），只能靠 CI 花十几分钟告诉你。所以把它
    变成一条本地就差得出来的静态检查：拿 ``ast`` 扫这个文件自己，凡是直接碰控件的测试
    函数，函数体里必须出现 ``_tk_root()`` 或者 ``_isolated_window(``（后者内部自己确认）。

    故意**不用 tkinter**，这样它恰好在出问题的那条腿上也会跑。
    """
    needs_display = (
        "G.theme_icon(", "G.RelayCheckApp(", "G.ExactProgress(",
        "tk.Toplevel(", "tk.PhotoImage(", ".winfo_",
    )
    own_name = "test_every_test_that_touches_a_widget_confirms_there_is_a_display_first"
    text = Path(__file__).read_text(encoding="utf-8")
    lines = text.splitlines()

    examined = 0
    unguarded: list[str] = []
    for node in ast.parse(text).body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        if node.name == own_name:
            # 这条测试自己的函数体里就写着那串标记，扫自己没有意义。
            continue
        body = "\n".join(lines[node.lineno - 1:node.end_lineno])
        if not any(marker in body for marker in needs_display):
            continue
        examined += 1
        if "_tk_root()" not in body and "_isolated_window(" not in body:
            unguarded.append(node.name)

    assert examined >= 18, f"只认出 {examined} 条碰控件的测试，扫描逻辑可能失效了"
    assert not unguarded, (
        "这些测试碰了 tk 控件却没确认有显示器，在没有 DISPLAY 的机器上会炸："
        + "、".join(unguarded)
        + " —— 加上 `if _tk_root() is None: _skip(...); return`，"
          "或者改用 _isolated_window()"
    )


def test_the_icon_is_drawn_pixel_by_pixel_and_not_borrowed_from_a_font() -> None:
    """☀ 和 ☾ 不在 Microsoft YaHei UI 里。

    Tk 找不到字形就回退到别的字体，回退字体的行高不一样，而标题那一行是手工对齐
    的——一个字符能让整行错位。所以太阳和月亮只能自己画，16×16，纯 stdlib。
    """
    if not _need_gui():
        return
    if _tk_root() is None:
        # tkinter 装上了但没有显示器时，PhotoImage 会因为没有默认 root 直接抛
        # ``RuntimeError: Too early to create image``。这台机器上有显示器，所以
        # 只有 CI 的 ubuntu 腿会走到这里 —— 忘了这道判断，代价是本地全绿而远端红。
        _skip("no display available")
        return
    ink, page = "#c9297a", "#ffffff"
    sun = G.theme_icon("sun", ink, page)
    moon = G.theme_icon("moon", ink, page)
    for image in (sun, moon):
        assert isinstance(image, G.tk.PhotoImage)
        assert (image.width(), image.height()) == (16, 16), "图标必须正好 16×16"

    lit = (0xC9, 0x29, 0x7A)
    blank = (0xFF, 0xFF, 0xFF)
    assert sun.get(8, 8) == lit, "太阳中心是空的"
    assert moon.get(8, 8) == blank, "月亮中心是实的——那不是月亮，是圆点"
    # 「能活过 16px 的才叫 icon」：没有光芒的圆盘在 16px 下就是一颗状态指示灯。
    assert sun.get(8, 0) != blank, "太阳没有光芒，16px 下看不出是太阳"
    assert moon.get(3, 3) != blank, "月亮被咬空了"


def test_every_canvas_ramp_keeps_text_readable() -> None:
    """画布渐变是背景，不是装饰：它不许吃掉任何一段文字的对比度。

    Tk 没有 CSS 的 ``linear-gradient``，所以渐变是「按 y 采样出来的若干个纯色
    帧」。文字压在帧上的对比度必须按最坏的那一帧算，也就是渐变的两端。
    """
    if not _need_gui():
        return
    for name, theme in G.THEMES.items():
        stops = (theme["canvas_ramp_top"], theme["app_bg"], theme["canvas_ramp_bottom"])
        for stop in stops:
            body = _contrast(theme["text"], stop)
            assert body >= 7.0, f"{name}: 正文压在 {stop} 上只有 {body:.2f}:1"
            muted = _contrast(theme["text_muted"], stop)
            assert muted >= 4.5, f"{name}: 次要文字压在 {stop} 上只有 {muted:.2f}:1"
        # 渐变是「画布泛一层色」，不是条纹。端点和画布本色离得太远就成色带了。
        assert theme["canvas_ramp_top"] != theme["canvas_ramp_bottom"]
        for stop in (theme["canvas_ramp_top"], theme["canvas_ramp_bottom"]):
            band = _contrast(stop, theme["app_bg"])
            assert band < 1.30, f"{name}: {stop} 和画布本色差到 {band:.3f}:1，成色带了"


def test_the_canvas_is_painted_as_a_ramp_and_not_as_a_block_per_container() -> None:
    """一块平色不算渐变。

    第一版就是那样：给每个画布容器取它自己中心的 y，于是每个 pane 是一整块平色，
    两块的交界处成了一条硬边 —— 看着像渲染坏了，不像渐变。这条测试绕开截图，直接读
    Canvas 上真正画出来的横线，要求颜色随 y 一路变过去。
    """
    with _isolated_window("light") as (top, app, _tmp):
        top.geometry("980x800")
        top.update()
        app._paint_ramp()
        top.update_idletasks()

        origin = top.winfo_rooty()
        stamps: list[tuple[float, str]] = []
        for canvas in app._ramp_canvases:
            offset = canvas.winfo_rooty() - origin
            bands = sorted(
                (float(canvas.coords(item)[1]), float(canvas.coords(item)[3]), item)
                for item in canvas.find_withtag("ramp")
            )
            # 块与块之间不许留缝：留了缝就露出画布本色，出来的是条纹纸不是渐变。
            for (_y0, y1, _one), (ny0, _ny1, _two) in zip(bands, bands[1:]):
                assert y1 >= ny0, f"画布在 y={y1} 和 {ny0} 之间空了 {ny0 - y1}px"
            for y0, _y1, item in bands:
                stamps.append(
                    (offset + y0, str(canvas.itemcget(item, "fill")).lower())
                )
        stamps.sort()
        assert len(stamps) > 50, f"整块画布只画了 {len(stamps)} 段，不成斜坡"

        steps: list[tuple[float, str]] = []
        for stamp in stamps:
            if not steps or steps[-1][1] != stamp[1]:
                steps.append(stamp)
        assert len(steps) >= 16, (
            f"整块画布只有 {len(steps)} 档颜色，这是几块平色而不是渐变: "
            f"{[colour for _y, colour in steps]}"
        )
        assert steps[0][1] == G.PALETTE["canvas_ramp_top"].lower(), steps[0]

        # 两套主题都是上浅下深：浅色是「浅灰泛粉」，深色是「黑泛暗红」。
        lumas = [_luma(colour) for _y, colour in steps]
        drops = [round(lumas[i - 1] - lumas[i], 6) for i in range(1, len(lumas))]
        assert min(drops) >= 0, f"渐变中途变亮了：{drops} 里的 {min(drops)}"
        assert sum(drops) > 0.05, f"整条坡只降了 {sum(drops):.5f}，肉眼看不出来"


def test_the_theme_is_remembered_in_a_file_that_holds_nothing_else() -> None:
    """窗口上印着「Key 不落盘」。记住主题的这一行字不能把这句话捅破。

    所以那个文件只准有一个键，而且这里顺手钉死它不会顺手把地址和 Key 也写进去。
    """
    with _isolated_window("light") as (_top, app, tmp):
        app.url_var.set("https://relay.example.test/v1")
        app.key_var.set("sk-must-never-be-written-to-disk")
        app._toggle_theme()

        path = tmp / "ui.json"
        assert path.is_file(), "切了主题却没记住"
        raw = path.read_text(encoding="utf-8")
        assert json.loads(raw) == {"theme": "dark"}, raw
        assert "sk-" not in raw, "主题文件里出现了 Key"
        assert "relay.example" not in raw, "主题文件里出现了目标地址"
        assert [p.name for p in tmp.iterdir()] == ["ui.json"], "多写了别的文件"
        assert G.load_theme() == "dark"


def test_the_design_document_lists_the_colours_that_are_in_the_code() -> None:
    """``gui/DESIGN.md`` 的色表必须和模块里的两套调色板逐字相同。

    这份文档存在的唯一理由是「什么叫改对了」，而被它坑过的方式也只有一种：代码
    改了、文档还写着旧值，于是一条已经不存在的规则继续被人遵守。手抄 38 行十六
    进制数字正是会这样漂的东西，所以让测试来抄。

    故意**不用 tkinter**（``ast`` 直接解析源码），这样没有显示器的 CI 腿也真跑。
    """
    source = (ROOT / "gui" / "relaycheck_gui.py").read_text(encoding="utf-8")
    doc = (ROOT / "gui" / "DESIGN.md").read_text(encoding="utf-8")

    palettes: dict[str, dict[str, object]] = {}
    chrome: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in {"LIGHT", "DARK"}:
                palettes[node.target.id] = ast.literal_eval(node.value)
        elif isinstance(node, ast.Assign):
            if any(
                isinstance(t, ast.Name) and t.id == "_CHROME_ROLES" for t in node.targets
            ):
                chrome = list(ast.literal_eval(node.value))
    assert set(palettes) == {"LIGHT", "DARK"}, f"解析不出两套调色板: {sorted(palettes)}"
    assert chrome, "找不到 _CHROME_ROLES"

    rows = re.findall(
        r"^\| `([a-z_]+)` \| `(#[0-9a-f]{6})` \| `(#[0-9a-f]{6})` \|",
        doc,
        re.MULTILINE,
    )
    assert len(rows) == len(chrome), (
        f"DESIGN.md 的色表里有 {len(rows)} 行，代码里有 {len(chrome)} 个 chrome 角色"
    )

    listed = {role for role, _, _ in rows}
    assert listed == set(chrome), (
        f"文档与代码的角色对不上 —— 只在文档里: {sorted(listed - set(chrome))}；"
        f"只在代码里: {sorted(set(chrome) - listed)}"
    )
    for role, light, dark in rows:
        assert palettes["LIGHT"][role] == light, (
            f"{role} 的浅色：文档 {light}，代码 {palettes['LIGHT'][role]}"
        )
        assert palettes["DARK"][role] == dark, (
            f"{role} 的深色：文档 {dark}，代码 {palettes['DARK'][role]}"
        )


def test_the_design_document_does_not_name_things_that_no_longer_exist() -> None:
    """``gui/DESIGN.md`` 里点名的每个标识符，都得在 `gui/` 里真的找得到。

    上一条盯的是**值**，这条盯的是**名字**。名字才是这份文档真正坑人的地方：它上一版
    里写着 `_ACCENT_HOVER`、`_SEVERITY_EMPTY_FG`、`_FAMILY_FALLBACK`、行号 —— 全都
    已经不存在了，而文档还在要求别人遵守它们。留一条不存在的规则，比不写更糟。

    允许两个豁免表，两者都必须短、必须写清理由。``KNOWN_GONE`` 是**故意**提到的历史
    名字（旁边就写着现在叫什么）；``NOT_OURS`` 是 Win32 / Tk / 标准库的名字，它们不在
    这个仓库里，也不该在。
    """
    known_gone = {
        "_ACCENT_HOVER", "_SEVERITY_EMPTY_FG", "_FAMILY_FALLBACK", "_FAMILY_COLORS",
        "_SASH_HINT", "VERDICT_STYLE", "_CARD_ACCENT", "accent_hover", "on_accent",
        "_ramp_widgets", "_ramp_last",
    }
    not_ours = {
        "AA", "AAA", "CopyFromScreen", "EnumWindows", "IsWindowVisible", "PrintWindow",
        "GetWindowTextLengthW", "GetWindowDC", "CreateCompatibleDC", "SelectObject",
        "GetDIBits", "create_window", "ctypes", "frombytes", "frombuffer",
    }

    doc = (ROOT / "gui" / "DESIGN.md").read_text(encoding="utf-8")
    haystack = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted((ROOT / "gui").glob("*.py"))
    )
    haystack += (ROOT / "tests" / "test_gui.py").read_text(encoding="utf-8")

    is_identifier = re.compile(r"^_?[A-Za-z][A-Za-z0-9_]*$")
    spans = sorted({s for s in re.findall(r"`([^`\n]+)`", doc) if is_identifier.match(s)})
    assert len(spans) > 80, f"只提取到 {len(spans)} 个标识符，正则或文档结构变了"

    unknown = [s for s in spans if s not in known_gone and s not in not_ours
               and s not in haystack]
    assert not unknown, (
        "DESIGN.md 里点到了这个仓库里不存在的东西："
        + "、".join(unknown)
        + " —— 要么改名，要么加进 known_gone 并写明现在叫什么"
    )


def test_building_the_window_never_writes_a_theme_file() -> None:
    """构造窗口只**读**偏好，写只发生在真的按了那个开关之后。

    这条同时守着两件事。一是本文件那 19 个不传 ``theme=`` 的测试：它们的颜色必须来自
    默认主题，而不是来自这台机器上最后一个人点过的主题 —— 否则一套测试在这台机器上
    绿、在那台机器上红。二是「Key 不落盘」那句承诺：窗口自己建起来就动用户的 home
    目录，是那种没人会去查的越界。
    """
    if not _need_gui():
        return
    assert _HERMETIC_THEME_DIR is not None
    assert G.load_theme() == G._DEFAULT_THEME, (
        "模块级隔离没生效：load_theme() 读到了真实用户目录里的选择"
    )

    with _isolated_window("light") as (_top, _app, tmp):
        assert not (tmp / "ui.json").exists(), "只是把窗口建起来就写了一个主题文件"


# --------------------------------------------------------------------- runner


def _main() -> int:
    if G is None:
        print(f"skip: tkinter unavailable ({_TK_ERROR})")
        return 0

    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    passed = skipped = failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "Skipped":
                skipped += 1
                print(f"  skip {name}: {exc}")
                continue
            failed += 1
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"  ok   {name}")
    print(f"\n{passed}/{len(tests)} passed, {skipped} skipped")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_main())
