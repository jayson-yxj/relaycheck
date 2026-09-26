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
        assert "INFO=7" in app.chips_label["text"]
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
        app._show_card("检测到中等问题", "说明", {"medium": 1}, ["  ! m  售卖 a，自称 b"])
        assert "自称 b" in app.family_label["text"], app.family_label["text"]
        assert "家族线索" in app.family_label["text"]

        # Every path with no family data must clear it — including 「正在检测…」
        # and 「运行失败」, which never call _family_lines at all.
        app._show_card("运行失败", "说明", None)
        assert app.family_label["text"] == "", app.family_label["text"]
    finally:
        root.update_idletasks()


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
