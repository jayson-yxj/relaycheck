# relaycheck 桌面版（Windows exe）

给不装 Python、也不用命令行的人一个能双击跑的东西。

```powershell
.\gui\build.ps1
```

脚本会在仓库根目录建一个独立的 `.venv-build`（不碰你平时的环境），装好
`requests` + `pyinstaller`，然后按 `gui\relaycheck_gui.spec` 构建。加 `-Test` 会
顺带跑一遍下面那个端到端比对。

产物在 `dist\relaycheck-desktop\`：

| 文件 | 作用 |
| --- | --- |
| `relaycheck-gui.exe` | 窗口程序，给人双击的 |
| `relaycheck.exe` | 控制台程序，**GUI 只用它来跑审计**，不用手动开 |
| `_internal\` | Python 运行时、tcl/tk、requests —— **必须跟着一起拷，缺了 exe 起不来** |

打包本机实测 **29.6 MB**（整个目录，989 个文件），两个 exe 分别 2.68 MB / 2.66 MB，
其余是 Python 运行时、tcl/tk、OpenSSL 和 requests；压成 zip 约 **14–15 MB**
（本机构建 14.2 MB，GitHub Release 上构建出来的 15.0 MB —— 两边 PyInstaller 版本略有差异）。

---

## 怎么发给别人

两条路，按对方会不会用命令行分。

**一、从 GitHub Release 下载（推荐，对方什么都不用装）。**
打 `v*` tag 时 `.github/workflows/desktop.yml` 会在 `windows-latest` 上构建、跑一遍
`gui\e2e_bundle.py`，然后把 `relaycheck-<版本>-windows-x64.zip` 挂到 Release 页。
对方解压 → 打开 `relaycheck-desktop\` → 双击 `relaycheck-gui.exe`。

想在发布前先拿一个：Actions → desktop → Run workflow，产物在同一次运行的 Artifacts 里。
这个工作流和 `release.yml`（PyPI）**互相独立** —— PyPI 那边没配好，不影响 exe 的下载。

**二、自己构建**：`.\gui\build.ps1`（见下）。sdist 里也带着 `gui/`（含图标文件），所以只有源码包的人
同样能构建，不用克隆仓库。`tests\test_gui.py` 里有一条测试专门钉住这件事 —— 0.1.4 的 sdist
就是因为漏了两个图标文件而**完全构建不出来**。

---

## 图标是生成出来的，不是画出来的

`gui/make_icon.py` 从源码里的几何参数生成 `relaycheck.ico`（16/24/32/48/64/128/256 七档）和
`relaycheck.png`（Tk 的 `iconphoto` 读不了 ico，窗口那一侧要用 png）。改了设计就重跑它，
不要手改这两个二进制文件。

设计上只有两条硬约束，写在生成器的 docstring 里：**必须活过 16px** —— 资源管理器的小图标视图
和小任务栏用的就是这一档，只在 256px 好看的那叫插画；以及**不能被读成又一个安全盾牌**。
放大镜和双向箭头两版都是在 16px 上被淘汰的。

`relaycheck_gui.spec` 里两个 exe 都设了 `icon=`，png 额外进 `datas` 供窗口使用。
**这两个文件必须进 sdist**（`MANIFEST.in` 的 `recursive-include gui` 覆盖了 `*.ico *.png`）：
图标路径不存在时 PyInstaller 抛的是 `FileNotFoundError`，不是退化成没图标，而是根本构建不出来。

---

## 为什么是两个 exe

窗口程序没有控制台，PyInstaller 会把 `sys.stdout` / `sys.stderr` 置成 `None`。
`relaycheck.cli` 里到处是 `print()`，在窗口进程里直接跑就会
`AttributeError: 'NoneType' object has no attribute 'write'`。

给审计引擎单独一个控制台子系统的 exe，问题从根上没了：GUI 用
`CREATE_NO_WINDOW` + 管道启动它，**看不见黑框**，而 CLI 手里是一个真能写的流。
两个 exe 共用同一个 `COLLECT`，所以 Python 运行时和 tcl/tk 只存一份，体积没有翻倍。

`relaycheck-gui.exe --run-audit ...` 是个后备通道：万一同目录下找不到
`relaycheck.exe`，窗口进程会给自己重新接上 stdout 再跑 CLI。`tests/test_gui.py`
和 `gui\e2e_bundle.py` 两条路都测。

## GUI 不重写审计逻辑

界面只把表单拼成**和命令行完全一样的 argv**，交给子进程跑，然后读
`report.json` 渲染结论卡片。审计逻辑一行都不在这儿 —— 所以界面永远不可能给出
和命令行不一样的结论，而钉住命令行的那 15 项端到端验收测试也就等于钉住了整个
工具。

两个刻意的选择：

* **API Key 走环境变量，绝不进命令行。** Windows 上任何进程都能通过 WMI 读到别的
  进程的命令行，把 key 放那儿等于全机器可见。日志区还会把 key 打码 —— 窗口是能
  截图的。
* **审计跑在子进程里，不是线程。** 线程停不干净：正卡在 60 秒 socket 超时里的线程
  没法中断，所谓的「停止」按钮就是个假的。子进程可以直接 `terminate()`。

## 一个踩过的坑：frozen 程序不认 `PYTHONIOENCODING`

实测：父进程给子进程设了 `PYTHONIOENCODING=utf-8`，**打包后的 exe 照样按 GBK 输出**
（`目标` 变成 `\xc4\xbf\xb1\xea`）。GUI 按 UTF-8 读管道，结果就是日志窗口满屏乱码，
而 `report.json` 完全正确 —— 最难查的那种 bug，文件是好的，用户盯着看的那块是坏的。

修法是在自己的代码里把流强制成 UTF-8（`relaycheck.cli.force_utf8_output`），外部
改不掉。真控制台上这等于没改：Python 3.6+ 在 Windows 控制台走 `WriteConsoleW`，
本来就报 `utf-8`，手动跑 `relaycheck.exe` 的人看不出区别。只有管道和重定向文件
受影响 —— 正是坏掉的那一种。

---

## 分发前必须知道的三个坑

这三条没有免费解法，别指望打包工具能绕过去。

### 1. 杀软误报

PyInstaller 的 onefile 模式会自解压到 `%TEMP%\_MEIxxxxx`，**这正是恶意打包器的
行为特征**。360、火绒、Defender 都可能拦截或直接删掉 exe。

本 spec 用的是 **onedir**（一个目录 + `_internal\`），并且 **关掉了 UPX 压缩**
（`upx=False`）—— UPX 壳会显著抬高误报率。宁可大几 MB。

用户侧的临时办法是加白名单；但真要发布得做代码签名。

### 2. SmartScreen

没有代码签名证书，用户第一次双击会看到 **「Windows 已保护你的电脑」**，要再点
「更多信息」→「仍要运行」。证书一年大概 $100–400，另外还需要积累下载量才能建立
信誉。**这一个弹窗就足以劝退大部分小白**，是目前最大的落地障碍。

### 3. 路径尽量别带中文

**没有实测过**：本次构建全程在纯 ASCII 路径下完成，所以这一条是提醒不是结论。
PyInstaller 的 bootloader 和 tcl/tk 在非 ASCII 路径上出过问题，历史上不太稳。

构建路径和**用户解压路径**都建议用纯英文短路径（比如 `C:\relaycheck\`）。
真要在中文路径下构建，构建完务必跑一遍 `gui\e2e_bundle.py` —— 它会跑起真的 exe，
路径问题在这一层才看得出来。

---

## 环境要求

打包机需要：

* Windows
* Python 3.9+，**并且带 tkinter**（官方安装包默认带；部分精简版/conda 基础环境要单独确认）
* 能联网装 `requests` 和 `pyinstaller`

`build.ps1` 会在仓库根目录建一个 `.venv-build` 专门用来打包，不污染你平时的环境。

跑起来之后，exe 本身只依赖 Windows 10 及以上，**目标机器不需要装 Python**。

## 测试

分两层，都不碰真站。

**`tests\test_gui.py`** —— 20 项，不需要构建产物，CI 的五条腿都会跑（没有 tkinter
的机器，比如 headless Linux runner，会干净地 skip 掉）。它钉的是界面自己决定的东西：

* **key 绝不进命令行。** 把 `subprocess.Popen` 换掉，真的跑一次
  `AuditProcess.start()`，然后检查 argv 和 env —— 一个命令行嗅探器能看到的东西。
* **窗口通道不能断。** `--run-audit` 必须绕过 Tk 直达 CLI，退出码 0/1/2 不能串。
* **结论卡片不能和 reporter 跑偏。** 五个 verdict 字符串是从
  `reporter.Report.verdict` 的源码里读出来的，改词就会红，而不是默默渲染出一张没
  颜色没解释的卡片。
* **进度条上的数必须是 CLI 报过的。** 拿真实的进度行喂进去，断言条只走到已完成的项，
  中途停止不会被补满；顺手钉住「花钱提醒是粗体且不在按钮行」。
* **图标真的挂上了。** 直接解析 ICO 头校验七档尺寸，不依赖 Pillow（CI 只装
  `requests` + `pytest`）。
* **构建脚本和 sdist 的两个环境坑。** `build.ps1` 必须有 UTF-8 BOM；`MANIFEST.in` 必须
  覆盖 spec 引用到的每种资产后缀。这两条在 CI 的 en-US runner 上都**不会**报错，
  所以只能这样钉死。

```powershell
python tests\test_gui.py
```

**`gui\e2e_bundle.py`** —— 需要先构建。起本地 mock 中转站，用源码 CLI、引擎 exe、
窗口 exe 的 `--run-audit` 通道跑**同一份审计**，再逐字段比对结论（退出码、verdict、
严重度分布、finding id 列表、受测模型）。掺了 exe 才有的编码和 stdout 问题，全在这
一层暴露。

```powershell
.venv-build\Scripts\python.exe gui\e2e_bundle.py
```
