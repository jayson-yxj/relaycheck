"""End-to-end proof that the bundled executables judge exactly like the CLI.

Not a unit test, and not part of CI: it needs a built bundle. It starts the real mock
relay, runs the *same* audit three ways against it, and compares the resulting reports
field by field:

  1. ``source``          ``python -m relaycheck.cli``
  2. ``engine-exe``      ``dist/relaycheck-desktop/relaycheck.exe``            (console)
  3. ``gui-redispatch``  ``dist/relaycheck-desktop/relaycheck-gui.exe --run-audit``

(3) is the one that earns its keep. It is a *windowed* executable, so PyInstaller
leaves ``sys.stdout`` as ``None``; it only works because the GUI repairs the stream
and forces UTF-8 before touching the CLI. If either ever regresses, this run dies with
``AttributeError: 'NoneType' object has no attribute 'write'`` or prints mojibake —
both of which are silent for anyone who only ever runs ``relaycheck.exe`` by hand.

Build first, then run::

    .\\gui\\build.ps1 -Test
    # or directly, with the build venv:
    .venv-build\\Scripts\\python.exe gui\\e2e_bundle.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist" / "relaycheck-desktop"

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from relaycheck.cli import _make_output_safe  # noqa: E402

_make_output_safe()

# Same reason as in the test files: judging a UTF-8 report on a cp1252 console is a
# crash waiting to happen.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

CREATE_NO_WINDOW = 0x08000000

_ok = 0
_bad = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global _ok, _bad
    if cond:
        _ok += 1
        print(f"  ok   {label}")
    else:
        _bad += 1
        print(f"  FAIL {label} {extra}")


def _runners() -> dict:
    return {
        "source": lambda args: [sys.executable, "-m", "relaycheck.cli", *args],
        "engine-exe": lambda args: [str(DIST / "relaycheck.exe"), *args],
        "gui-redispatch": lambda args: [str(DIST / "relaycheck-gui.exe"), "--run-audit", *args],
    }


def run_one(runner: str, scenario: str, port: int, out_root: Path) -> dict:
    """Serve a fresh mock, run one audit through ``runner``, return report.json."""
    import mock_relay

    httpd = mock_relay.serve(port, scenario)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    out_dir = out_root / f"{scenario}-{runner}"
    out_dir.mkdir(parents=True, exist_ok=True)

    args = [
        "-u", f"http://127.0.0.1:{port}",
        "--out-dir", str(out_dir),
        "--max-models", "2",
        "--reliability-samples", "2",
        "--delay", "0",
        "--budget", "60",
    ]
    env = dict(os.environ)
    # The key goes in the environment, exactly like the GUI does it. This also proves
    # the child really reads RELAYCHECK_API_KEY rather than silently running unauthed.
    env.pop("OPENAI_API_KEY", None)
    env["RELAYCHECK_API_KEY"] = mock_relay.TEST_API_KEY
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = subprocess.run(
            _runners()[runner](args),
            cwd=str(REPO),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    finally:
        httpd.shutdown()
        httpd.server_close()

    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    report["_exit"] = proc.returncode
    report["_stdout"] = proc.stdout
    # U+FFFD is what the reader produced for undecodable bytes; 锟 is the classic
    # GBK/UTF-8 double-decode artefact. Either one means the child printed the wrong
    # encoding and this run is not trustworthy.
    report["_mojibake"] = "\ufffd" in proc.stdout or "锟" in proc.stdout
    return report


def summarise(report: dict) -> tuple:
    """The part of a report that must be identical across runners.

    Deliberately excludes timing, request counts and the out-dir, which legitimately
    differ run to run, and keeps everything that is a *judgement*.
    """
    return (
        report["_exit"],
        report["verdict"],
        report["severity_counts"],
        sorted(f["id"] for f in report["findings"]),
        report["models_tested"],
    )


def main(argv=None) -> int:
    if not (DIST / "relaycheck-gui.exe").exists() and not (DIST / "relaycheck-gui").exists():
        print(f"没有找到构建产物：{DIST}")
        print("先跑  .\\gui\\build.ps1")
        return 2

    out_root = Path(tempfile.mkdtemp(prefix="relaycheck-e2e-"))
    try:
        for scenario in ("fraudulent", "clean"):
            print(f"\n--- scenario: {scenario} ---")
            port = 8300 if scenario == "fraudulent" else 8301
            baseline = None
            for runner in _runners():
                report = run_one(runner, scenario, port, out_root)
                if runner == "gui-redispatch" and report["_stdout"].strip() == "":
                    check("gui-redispatch: windowed child still produced output", False,
                          "empty stdout")
                check(f"{runner}: no mojibake in CLI output", not report["_mojibake"],
                      repr(report["_stdout"][:120]))
                check(f"{runner}: report has a verdict", bool(report.get("verdict")),
                      repr(report.get("verdict")))
                summary = summarise(report)
                if baseline is None:
                    baseline = summary
                    print(f"       baseline: exit={summary[0]} verdict={summary[1]} "
                          f"counts={summary[2]}")
                    print(f"       findings: {', '.join(summary[3])}")
                else:
                    check(f"{runner}: identical judgement to source", summary == baseline,
                          f"\n         source: {baseline}\n         {runner}: {summary}")

            # The scenario's own contract, asserted against the bundled engine — the
            # thing a user of the exe will actually run.
            engine = run_one("engine-exe", scenario, port, out_root)
            counts = engine["severity_counts"]
            if scenario == "fraudulent":
                check("fraudulent still exits 1 (findings)", engine["_exit"] == 1,
                      f"exit={engine['_exit']}")
                check("fraudulent still has a CRITICAL", counts.get("critical", 0) >= 1,
                      repr(counts))
            else:
                check("clean still exits 0", engine["_exit"] == 0, f"exit={engine['_exit']}")
                check("clean still reports zero findings above INFO",
                      all(counts.get(k, 0) == 0 for k in ("critical", "high", "medium", "low")),
                      repr(counts))
                check("clean's verdict is the reassuring one",
                      engine["verdict"] == "未检测到问题", repr(engine["verdict"]))
    finally:
        shutil.rmtree(out_root, ignore_errors=True)

    print(f"\n{_ok}/{_ok + _bad} passed")
    return 0 if _bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
