# relaycheck 桌面版（Windows exe）

给不装 Python、也不用命令行的人一个能双击跑的东西。

改外观之前先读 `DESIGN.md`（颜色、排版、动效的规则）；仓库根的 `AGENTS.md` 是总约束。

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

## 不发请求的界面预览

改布局、颜色或动效时，不必拿真实 API Key 跑一遍。`preview_ui.py` 把静态假数据交给
正式窗口的渲染方法，只画界面，不创建客户端、不发送请求、不写报告：

```powershell
python gui\preview_ui.py initial
python gui\preview_ui.py advanced
python gui\preview_ui.py running
python gui\preview_ui.py clean
python gui\preview_ui.py problem
python gui\preview_ui.py failure
python gui\preview_ui.py clean --theme dark
```

`--theme` 把调色板钉死。不钉死的话预览会跟着 `~/.relaycheck/ui.json` 里上一位使用者
存的选择跑，深色浅色的回归截图就没法比了。

它不是第二套报告逻辑；结论文案、严重度徽章和家族线索仍由 `RelayCheckApp` 原来的方法绘制。

## 测试

分两层，都不碰真站。

**`tests\test_gui.py`** —— 46 项，不需要构建产物，CI 的五条腿都会跑（没有 tkinter
的机器，比如 headless Linux runner，会干净地 skip 掉）。它钉的是界面自己决定的东西：

* **key 绝不进命令行。** 把 `subprocess.Popen` 换掉，真的跑一次
  `AuditProcess.start()`，然后检查 argv 和 env —— 一个命令行嗅探器能看到的东西。
* **窗口通道不能断。** `--run-audit` 必须绕过 Tk 直达 CLI，退出码 0/1/2 不能串。
* **结论卡片不能和 reporter 跑偏。** 五个 verdict 字符串是从
  `reporter.Report.verdict` 的源码里读出来的，改词就会红，而不是默默渲染出一张没
  颜色没解释的卡片。
* **卡片不能替使用者下结论。** 计数为 0 的严重度徽章不上色（实心红的 `CRITICAL 0`
  是个警报）；没查成的卡片不显示「问题数量」那一段，而不是显示一排 0。家族线索里
  上色的**只有自称厂商那一段**，「售卖 …」那半句必须保持无色——给整行上色会把正在被
  核对的名字染成「自称」的颜色，含义正好反了。
* **进度条上的数必须是 CLI 报过的。** 拿真实的进度行喂进去，断言条只走到已完成的项，
  中途停止不会被补满；顺手钉住「花钱提醒是粗体且不在按钮行」。
* **收起不等于重置。** 「高级」默认收起，展开再收起之后里面的值必须还在——把设置藏起来又
  悄悄改回默认，等于替使用者做了他没做的决定，而花的是他的额度。
* **贵的那个选项得说清贵在哪。** 两个强度标签里的 8 / 11 不是从探针注册表算出来的，所以
  注册表多一项、窗口还照样说着旧数字——而这是使用者掏钱前唯一会看的地方。另一条测试从
  反面钉住 `context` **不在**默认集里：默认集跑在「第一次面对一个没人量过的站」那一刻，
  那是最不该把别人的钱花在全工具最贵单项实验上的时机。彻底的默认会被学会绕开，便宜的
  默认才会一直被跑。
* **交出去的结论不含 key。** 「复制结论」把卡片内容加一段页脚送进剪贴板。它是整个窗口里唯一
  一个往窗口**之外**写东西的按钮（机器上别的程序也读得到，关掉窗口还在），所以有测试拿假
  key 走一遍剪贴板。
* **分界线不裁卡片。** 分界线必须在窗口真正布局之后才放——在 `__init__` 里调 `sashpos` 是
  静默空操作，ttk 收下数字、因为控件只有 1px 高而兑现不了、然后永久停在 0，上半格（整个
  表单，含「开始检测」）塌成 0 高。而且卡片变高时只往下推、从不往回拉：裁一半的家族块比
  不显示更糟，它读起来像「就这些」。
* **图标真的挂上了。** 直接解析 ICO 头校验七档尺寸，不依赖 Pillow（CI 只装
  `requests` + `pytest`）。
* **换主题不能丢东西。** 换主题是拆掉整棵控件树重建（tkinter 在构造时就把颜色烤进去
  了），所以它必须自己把使用者填的地址、Key、模型、输出目录，以及已经跑出来的结论、
  日志正文和进度值一并搬过去——搬丢一样，使用者的体验就是「点了一下深色，填的东西没
  了」。还有一条测试走 浅色→深色→浅色 一圈，要求落回**同一批颜色**：这是「重建」相对
  「遍历控件重新刷色」的全部理由，漏掉任何一个派生颜色的表现恰好是「大部分变了、这一
  块没变」。
* **画布上那条坡得真是坡。** Tk 没有 `linear-gradient`，渐变是每 6px 一段实心方块堆出
  来的。测试直接读 Canvas 上画出来的方块，要求颜色随 y 一路变过去、块与块之间不留缝
  （留缝就露出画布本色，出来的是一张条纹纸），并且每一段上压着的正文和次要文字都还在
  AA 门槛之上。
* **图标是自己画的，不是字体里的字符。** `☀` / `☾` 不在 `Microsoft YaHei UI` 里，Tk 会
  回退到别的字体、把标题那一行的高度顶歪；所以太阳和月亮是逐像素画出来的 16×16
  `PhotoImage`，测试断言它确实是 `PhotoImage`、确实是 16×16、而且两张不是同一张。
* **主题文件里只有主题。** 窗口上印着「Key 不落盘」，记住深浅的那一行字不能把这句话
  捅破：测试断言那个文件恰好只有一个键，且不含地址和 Key。
* **`DESIGN.md` 里的色表和代码逐字相同。** 那份文档存在的唯一理由是「什么叫改对了」，
  而它能坑人的方式只有一种：代码改了、文档还写着旧值，于是一条**已经不存在的规则**继续
  被人遵守。38 行十六进制数字手抄必然漂，所以让测试来抄——它用 `ast` 直接解析源码，
  **不用 tkinter**，所以没有显示器的 CI 腿也真跑。
* **`DESIGN.md` 也不许点到不存在的东西。** 上一条盯的是值，这一条盯的是名字——它上一版
  里写的 `_ACCENT_HOVER`、`_SEVERITY_EMPTY_FG`、`_FAMILY_FALLBACK` 全都已经不存在了。
  测试把文档里每个像标识符的 `` `code span` `` 拿去 `gui/` 里找，找不到就红；两张豁免表
  （故意提到的历史名字 / Win32 与 Tk 的名字）都短得能一眼看完。
* **一套测试的颜色不能取决于上一个人点过什么。** 不传 `theme=` 建窗口会去读
  `~/.relaycheck/ui.json`，本文件有 19 处是那么建的；所以模块导入时就把这个查找指到一个
  不存在的路径上，默认主题说了算。顺手钉住「只是把窗口建起来」不会写用户 home 目录——
  写只发生在真的按了那个开关之后。
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
