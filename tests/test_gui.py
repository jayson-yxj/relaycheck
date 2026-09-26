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

import inspect
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "gui"))

from relaycheck.cli import _make_output_safe  # noqa: E402

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

    missing = returned - set(G.VERDICT_STYLE)
    assert not missing, f"the GUI has no styling for: {sorted(missing)}"
    for verdict in returned:
        fg, bg, plain = G.VERDICT_STYLE[verdict]
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

    _, _, plain = G.VERDICT_STYLE["未检测到问题"]
    assert "不等于" in plain, plain
    assert "家族" in plain, plain


def test_the_failure_card_does_not_imply_a_verdict() -> None:
    """A crashed run must not look like a result, in either direction."""
    if not _need_gui():
        return

    _, _, plain = G._FAIL_STYLE
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
        card_bg = G.VERDICT_STYLE["未检测到问题"][1]

        for key in ("critical", "high", "medium", "low"):
            chip = app.chips[key]
            assert str(chip["background"]) == card_bg, f"{key} 计数为 0，不该上色"
            assert str(chip["foreground"]) == G._SEVERITY_EMPTY_FG, key
            assert chip["text"].endswith(" 0"), chip["text"]
        for key in ("info", "clean"):
            chip = app.chips[key]
            fill_bg, fill_fg = G._SEVERITY_FILL[key]
            assert str(chip["background"]) == fill_bg, key
            assert str(chip["foreground"]) == fill_fg, key
            assert str(chip["background"]) != card_bg, key
        assert "INFO=2" in app._card_text() and "CLEAN=7" in app._card_text()

        # The hairline has to belong to the card it sits on: the card background
        # changes with the verdict, and a fixed grey rule clashes on some of them.
        assert str(app.chips_rule["background"]) == G._shade(card_bg, 0.9)
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
    assert unknown in G._FAMILY_FALLBACK, unknown
    assert unknown == G.family_color("some-vendor-from-2030")
    # Two different unknown vendors should not collapse onto one slot every time.
    spread = {G.family_color(f"vendor-{i}") for i in range(12)}
    assert len(spread) > 1, "未知厂商全都撞到同一个颜色"
    assert all(colour in G._FAMILY_FALLBACK for colour in spread)


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
