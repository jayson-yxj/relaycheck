# 变更记录

本文件记录**行为**的变化，尤其是「严重程度」和「finding id」的语义变化——
一个已经发布过的 finding id 尽量不重用；如果它的含义变了，这里必须写明。

## [未发布]

- 文档：`gui/README.md` 里的桌面包体积改成实测区间（本机 14.2 MB，GitHub CI 构建 15.0 MB）。

## [0.1.1] — 2026-09-26

0.1.0 已经发布在 PyPI 上（2026-09-26），桌面版是打完那个 tag 之后才写的，所以它全部落在
这一节里。**PyPI 不允许复用版本号**，因此这一节对应的版本号是 0.1.1，不是 0.1.0。

### 新增：Windows 桌面版（`gui/`）

给不装 Python、也不用命令行的人一个能双击的东西。**界面不重写审计逻辑**：它把表单拼成
和命令行完全一样的 argv，交给子进程跑 `relaycheck.cli`，再读 `report.json` 渲染结论卡片。
界面因此永远不可能给出和命令行不一样的结论，钉住 CLI 的那 15 项端到端验收也就钉住了整个
工具。

两个刻意的设计：

- **API Key 走环境变量，绝不进命令行。** Windows 上任何进程都能通过 WMI 读到别的进程的
  命令行。日志区还会把 key 打码 —— 窗口是能截图的。
- **审计跑在子进程，不是线程。** 卡在 60 秒 socket 超时里的线程中断不掉，「停止」按钮
  会是个假的；子进程可以直接 `terminate()`。

打包用 PyInstaller 的 onedir 模式（不是 onefile）并关闭 UPX：这两条都是为了压低杀软误报率。
产物是**两个** exe —— 窗口程序没有控制台，PyInstaller 会把 `sys.stdout` 置成 `None`，
而 `relaycheck.cli` 里到处是 `print()`；给引擎单独一个控制台子系统的 exe 从根上解决，
两者共用同一个 `COLLECT`，体积不翻倍。

### 新增：Windows 桌面版直接在 Release 下载（`.github/workflows/desktop.yml`）

给不装 Python 的人一个 zip：打 `v*` tag 时在 `windows-latest` 上构建，跑一遍
`gui/e2e_bundle.py` 确认产物真的能跑（不然发布的只是一个 30 MB 的压缩包），再把
`relaycheck-<版本>-windows-x64.zip`（实测 14 MB）挂到 Release 页。解压 → 打开
`relaycheck-desktop\` → 双击 `relaycheck-gui.exe`。

刻意和 `release.yml` 分成两个工作流、互不依赖：往 PyPI 发布需要先在 pypi.org 配好
trusted publisher，而「PyPI 还没配好」绝不能连带让用户下不到一个能跑的 exe。
发布前想先拿一个：Actions → desktop → Run workflow，产物在 Artifacts。

### 修正：版本号只有一个来源

`pyproject.toml` 里写死的 `version` 和 `relaycheck/__init__.py` 里的 `__version__` 是两个
事实来源，而 `relaycheck --version`、报告里的 `tool_version`、GUI 标题读的都是后者。两边
一旦漂移，wheel 会以新版本号传上去、`relaycheck --version` 却还印旧号，而 `release.yml`
里那道「tag 与构建版本一致」的检查看的是 wheel 文件名，正好拦不住这种。

改成 `dynamic = ["version"]` 加
`[tool.setuptools.dynamic] version = {attr = "relaycheck.__version__"}`：以后发布只改
`relaycheck/__init__.py` 一处。实测把关起来的 `__version__` 临时改成 `9.9.9`，构建出来的
就是 `relaycheck-9.9.9-py3-none-any.whl`。

### 修正：frozen 程序不认 `PYTHONIOENCODING`

打包后的 exe 在管道/重定向下**按 GBK 输出中文**，即使父进程已经设了
`PYTHONIOENCODING=utf-8`。`report.json` 完全正确，只有控制台文字是坏的 —— 文件是对的、
用户盯着看的那块是错的，属于最难查的一类。新增
`relaycheck.cli.force_utf8_output()`，在自己的代码里把流强制成 UTF-8。

`_make_output_safe()` **行为未变**（它依旧只放松 `errors`，从不碰 `encoding`），所以已经
发布过的 pip CLI 行为一字未改。

### 测试

- 新增 `tests/test_gui.py`（12 项）：key 不进 argv（替换 `subprocess.Popen` 后真的跑一次
  `AuditProcess.start()`，检查 argv 和 env）、`--run-audit` 通道不被 Tk 挡住、
  五个 verdict 字符串直接从 `reporter.Report.verdict` 的源码读出以防卡片和报告跑偏。
  没有 tkinter 的机器（headless Linux runner）干净跳过，所以 CI 五条腿都能跑。
- 新增 `gui/e2e_bundle.py`（不在 CI 里，需要先构建）：起本地 mock，用源码 CLI、引擎 exe、
  窗口 exe 的 `--run-audit` 通道跑**同一份审计**，逐字段比对退出码 / verdict / 严重度分布 /
  finding id 列表 / 受测模型。实测 21/21。

### 文档

- README 增加「不装 Python：Windows 桌面版」一节，并把杀软误报、SmartScreen、体积三个
  分发障碍如实写出来 —— 这三条都没有免费解法，写清楚比让维护者踩一遍强。

## [0.1.0] — 2026-09-26

首个版本。11 个探针，8 个本地模拟场景，15 项端到端验收，外加 15 项模型选择单元测试。

### 探针

| 探针 | 查什么 |
|---|---|
| `reliability` | 可用性与延迟（先确认对方能不能干活） |
| `billing-panel` | 面板公开的上游成本 vs 实际计费（加价倍率） |
| `echo` | 响应体自报的 `model` 是否等于你请求的名字 |
| `tokenizer` | 固定串的 `prompt_tokens` 指纹，找出共享 tokenizer 的模型对 |
| `twins` | 开放性提示下逐字比对，找出输出完全相同的「双胞胎」 |
| `identity` | 直接问模型「你是谁」，与售卖名称对照 |
| `canary` | 注入唯一标记，验证提示词有没有原样送达 |
| `billing-hidden-reasoning` | 有没有把隐藏的思维链按输出价计费 |
| `params` | `max_tokens` / `stop` / `temperature` / `n` / `response_format` / `tools` 有没有被透传 |
| `stream` | 流式与完整返回是否一致 |
| `context` | 长输入有没有被静默截断（**唯一明显花钱的探针，默认不跑**） |

### 核心不变量

- **`CLEAN` 必须意味着「已经验证过」。** 「没测到」永远不能写成 `CLEAN`，
  它走 `*-000` 保留编号 + `INFO`。
- **「测不出来」不能写成「测出了问题」。** 空回复、该站自身不可复现、自述前后矛盾，
  都是 `INFO` 的「无法判定」，不是指控。
- **退出码 0 / 1 / 2 互不重叠**：`0` 没问题、`1` 有达到 `--fail-on` 的发现、`2` 这次没查成。
  崩溃一律是 `2`——绝不与「发现了问题」共用 `1`。
- **严格性分两档**：`severity`（有多严重）与 `confidence`（有多确定）分开，
  `LOW` 不触发默认的 `--fail-on high`。

### 修正（都是同一类错误的变体：报告高估或误读了自己测到的东西）

- `twins` 不再把「样本为空」算成「输出不稳定」——空输出分成 `blank` 与 `shaky` 两桶。
- `params` 的 `temperature` 检查改为三态（`honoured` / `ignored` / `inconclusive` /
  `nondeterministic`）：`temperature=0` 两次结果不一致时，结论是「该站不可复现」，
  而不是「参数被忽略」。
- `stream` 不再把「比较根本没跑成」写成 `stream-clean`。
- `tokenizer` / `twins` 遇到「声明同一个厂商」的模型对时降级为 `tok-101` / `twins-101`，
  不再把合法的别名对（`gpt-4o` / `gpt-4o-mini`）判成掉包。
- 推理模型的 `reasoning_content` 挤占 `max_tokens` 导致可见正文为空或只剩残片时，
  探针以更大的预算重试一次；仍拿不到就判「无法判定」，不判「不一致」。
- `echo-000` 不再用观测条数冒充「测到几个模型」（死站上这句话会自相矛盾）。
- `id-100` 的严重程度：`MEDIUM` → `LOW` → **`INFO`**。原因见下。

### `id-100` 为什么最终是 `INFO`

`id-100` 报告的是「模型自述的厂商与售卖名称不一致」。三次降级的理由是同一条：
**撑不起指控的东西不该出现在指控栏里。**

1. 它自己的文案就写着「必须由 `tokenizer` 指纹或行为比对独立指向同一结论」——
   那两条各自有 `CRITICAL` / `HIGH` 的 finding，它作为指控完全冗余。
2. 模型会幻觉自己的身份。非 OpenAI 的模型自称 OpenAI 是训练语料导致的常态，
   **诚实站上照样出现**：实测中一个正常转售 `deepseek-v4-flash` 的站，该模型两轮都
   稳定地自称 OpenAI。留成 `LOW` 的结果是**每一个转售 DeepSeek 的诚实站都白挨一条**。
3. 于是它降到与 `echo-101` / `tok-101` / `bill-201` 同档：**值得写进报告，
   不值得写进指控栏。**

同时加了一道门槛：**自述只有被重复一遍才算线索**——同一个问题问两遍，两次都点名同一个
外国厂商才输出 `id-100`；答得不一致降级为 `id-101`（`INFO`，措辞里明确写着「不指控掉包」）；
有一轮没答上来则记为 `id-102`。

### 文档

- 中文 `README.md` 与英文 `README.en.md` 逐节对应。
- `examples/` 四份报告由真实工具输出生成（`fraudulent` / `clean` / `dead` / `noisy`），
  不是手写的。
- 「六种结局」表把 `CLEAN` / 没跑成 / 没有可判定内容 / 该站不可复现 / 值得记录但不构成指控 /
  发现异常分开列清楚。

### 验收（`tests/test_mock_relay.py` 15 项端到端 + `tests/test_selection.py` 15 项单元）

- `fraudulent` 必须被抓出来：≥1 条 `CRITICAL`，且是具体的 finding id。
- `clean` / `same-vendor` / `slow` / `dead` / `reasoning` / `noisy` / `unstable-self`
  必须**零误报**（`same-vendor` 允许一条 `LOW` 别名提示）。
- `dead` 永远不能被报成 `CLEAN`。
- CLI 在 cp936 控制台下必须正常收尾并退出 `0`。
- 省略 `--models` 时，受测模型由 `/v1/models` 按**厂商多样性**选出，而不是照目录顺序取前 N 个
  （`tests/test_selection.py`）。
