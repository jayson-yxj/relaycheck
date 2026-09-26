# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec —— 一次构建，产出一个共享依赖的目录，里面两个 exe。

    relaycheck-gui.exe   窗口程序，给人双击用的
    relaycheck.exe       控制台程序，GUI 只用它来跑审计

为什么必须拆成两个 exe：窗口程序没有控制台，PyInstaller 会把
``sys.stdout`` / ``sys.stderr`` 置成 ``None``。``relaycheck.cli`` 里到处是
``print()``，一旦在窗口进程里直接跑就会 ``AttributeError: 'NoneType' object has
no attribute 'write'``。给引擎单独一个控制台子系统的 exe，问题从根上没了：GUI 用
``CREATE_NO_WINDOW`` + 管道启动它，看不见黑框，而 CLI 手里是一个真能写的流。

两者共用同一个 ``COLLECT``，所以 Python 运行时和 tcl/tk 只存一份，体积不会翻倍。

构建（用 gui\\build.ps1，它会自己准备好构建 venv）：

    .\\gui\\build.ps1

产物：``dist/relaycheck-desktop/``
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent

# 图标。`.ico` 给两个 exe 当外壳图标，`.png` 由窗口自己用 `iconphoto` 加载 —— Tk
# 读不了 ico。两份都由 `gui/make_icon.py` 从源码里的几何参数重新生成，不要手改二进制。
# 这不是纯装饰：PyInstaller 的默认图标本身就会抬高杀软的启发式评分（同 upx=False 的理由），
# 而且一个没有图标的 exe 在资源管理器里看起来就像来路不明的东西。
ICON = str(ROOT / "gui" / "relaycheck.ico")
ICON_PNG = str(ROOT / "gui" / "relaycheck.png")

# tkinter 的子模块是懒加载的，静态分析看不到，得点名。
HIDDEN = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.scrolledtext",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.font",
    "tkinter.constants",
]

# 探测用不到的大家伙。注意**不要**排除 email / http / urllib —— requests 依赖它们，
# 排掉会让 requests 在运行时 ImportError。
EXCLUDES = [
    "numpy", "pandas", "matplotlib", "scipy", "PIL", "cv2", "openpyxl",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "IPython", "jupyter", "notebook", "nbconvert", "nbformat",
    "sqlite3", "unittest", "test", "pydoc_data", "lib2to3",
    "distutils", "setuptools", "pip", "pytest", "doctest",
]

COMMON_ANALYSIS = dict(
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(ICON_PNG, ".")],
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

COMMON_EXE = dict(
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 压缩会显著抬高杀软误报率，宁可大一点
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)


def _merge(*tocs):
    """Concatenate TOCs, first occurrence of a name wins.

    Two separate Analyses each collect their own copy of the Python runtime, tcl/tk
    and requests. COLLECT does not promise to tolerate the duplicates, so drop them
    here rather than find out at build time.
    """
    seen, out = set(), []
    for toc in tocs:
        for entry in toc:
            if entry[0] in seen:
                continue
            seen.add(entry[0])
            out.append(entry)
    return out


# ------------------------------------------------------------------ the window app

a_gui = Analysis([str(ROOT / "gui" / "relaycheck_gui.py")], **COMMON_ANALYSIS)
pyz_gui = PYZ(a_gui.pure)
exe_gui = EXE(
    pyz_gui, a_gui.scripts, [],
    name="relaycheck-gui",
    console=False,
    icon=ICON,
    # 保持 True 的默认行为：窗口程序崩了会弹一个带 traceback 的对话框，
    # 而不是双击之后什么都没发生。
    disable_windowed_traceback=False,
    **COMMON_EXE,
)

# ----------------------------------------------------------------- the audit engine

a_cli = Analysis([str(ROOT / "gui" / "relaycheck_cli_entry.py")], **COMMON_ANALYSIS)
pyz_cli = PYZ(a_cli.pure)
exe_cli = EXE(
    pyz_cli, a_cli.scripts, [],
    name="relaycheck",
    console=True,
    icon=ICON,
    disable_windowed_traceback=False,
    **COMMON_EXE,
)

COLLECT(
    exe_gui,
    exe_cli,
    _merge(a_gui.binaries, a_cli.binaries),
    _merge(a_gui.datas, a_cli.datas),
    strip=False,
    upx=False,
    name="relaycheck-desktop",
)
