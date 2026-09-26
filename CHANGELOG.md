# 变更记录

本文件记录**行为**的变化，尤其是「严重程度」和「finding id」的语义变化——
一个已经发布过的 finding id 尽量不重用；如果它的含义变了，这里必须写明。

## [0.1.6] — 2026-09-27

### 桌面版结果卡分段：结论 / 问题数量 / 家族线索

原来这三块是左对齐堆在同一个色块里的，只靠换行分开。现在各成一段，每段前面一条 1px 细线。

- **不用 `ttk.Separator`**。卡片底色跟着 verdict 走（`#fee2e2` / `#fef3c7` / `#fef9c3` /
  `#dcfce7` / `#eeeeee`），而 ttk 控件不吃 `background`——分隔线会保留主题灰，在染色卡上
  像从别的窗口掉进来的一条线。改成 1px 高的 `tk.Frame`，颜色由卡片底色压暗 10% 算出来。
- **没查成就不显示这两段，而不是显示一排 0**。`CRITICAL 0 HIGH 0 …` 读起来是「我们量到了
  零个问题」，而「运行失败」这次什么都没量到。所以失败卡和启动状态只有结论那一段。

### 严重度改成彩色 chip

`CRITICAL=2   HIGH=2   MEDIUM=4   LOW=0   INFO=3   CLEAN=0` 这段等宽文本换成六个徽章。

- **计数为 0 的徽章绝不上色**（灰字、不填充）。实心红的 `CRITICAL 0` 是个警报，而且它偏偏
  出现在「未检测到问题」那张卡上——正好说反。填充＝这事发生了，灰＝没发生。
- 非零档各自填充：critical 深红 / high 橙 / medium 琥珀 / low 橄榄 / info 石板 / clean 绿。
- 卡片的纯文本镜像里仍是 `CRITICAL=2` 的写法，和报告、CLI 输出保持一致。

### 家族线索按自称厂商上色

只给**自称厂商那一小段**上色，同厂商同色。

- **不给整行上色**。整行上色会把「售卖 deepseek」——正在**被核对**的那个名字——也染成「自称」
  的颜色，含义正好反了。实测 `gpt-4o  售卖 openai，自称 anthropic` 里第一个 `openai` 是黑的，
  只有 `anthropic` 带色。
- 上色的用途是让「两个模型都自称 OpenAI」这种语料残留痕迹一眼可见，不必逐行读。
- **颜色是身份分组，不是定罪**：它只说「这两句是同一句」，不说「这个模型是假的」。卡片自己
  那句「只是线索，单独一条不足以定性」照旧保留。
- **颜色必须跨进程稳定**，所以不能用 `hash()`（按进程加盐，同一份报告每次跑颜色都不一样）。
  已知厂商查表，未知厂商用位置加权求和取模。测试直接从源码断言 `family_color` 里不出现
  `hash(`——这个 bug 在单个进程内是看不见的。
- 渲染控件从 `tk.Label` 换成 `tk.Text`：只有 Text 能给一行里的**一部分**上色。代价是多几行
  代码，换来引文可以选中复制——引文就是证据。

细节：

- `_show_card` 的第 4 个参数从「已经拼好的行」改成 `_family_rows()` 的
  `(prefix, reported, suffix)`，一份数据两种渲染（纯文本 / 带色），不可能漂移；测试断言
  Text 里的内容逐行等于 `_family_lines()`。
- 新增 `_card_text()`：整张卡的纯文本镜像。卡片现在由十几个控件拼成，测试再去逐个 `cget`
  等于在测布局而不是测内容。它也正是将来「复制结论」按钮要用到的那一个。
- 分段顺序靠手工 `before=` 维持：`pack` 只会往后追加，第二次跑的时候家族线索会跑到问题数量
  上面去。这个 bug 只在**第二次**运行才出现，看新开的窗口看不出来，所以有测试钉住它。
- 家族块的高度按**显示行**算，不按行数算：一条长引文会折行，按行数算会把引文尾巴裁掉，而裁掉
  的正好是这一段存在的理由。坑在于控件还没被布局时，Tk 会把「一个字符一行」当成显示行数
  （实测 48 字的单行被报成 47 行），所以只有等控件知道自己宽度之后才采信这个计数。

**测试**：`tests/test_gui.py` 20 → 25。

## [0.1.5] — 2026-09-27

### 修正：0.1.4 的 sdist 缺了两个图标文件，从源码包构建 exe 会直接失败

`MANIFEST.in` 里那行是 `recursive-include gui *.py *.ps1 *.spec *.md` —— 没有 `*.ico`。
于是 PyPI 上 `relaycheck-0.1.4.tar.gz` 的 `gui/` 里有 `build.ps1`、有 spec，**唯独没有
`relaycheck.ico` 和 `relaycheck.png`**。

这不是少了个图标。`relaycheck_gui.spec` 把这两个路径直接交给 PyInstaller，而图标路径不存在时
PyInstaller 抛的是：

```
File "PyInstaller/building/icon.py", line 34, in normalize_icon_type
    raise FileNotFoundError(f"Icon input file {icon_path} not found")
FileNotFoundError: Icon input file D:/.../gui/relaycheck.ico not found
```

—— 而且是在 `Copying icon to EXE` 那一步，前面几十秒的分析全白做。所以在 0.1.4 的 sdist 上
`gui/build.ps1` **一个 exe 也产不出来**。受影响的范围说明白：wheel 不含 `gui/`
（`include = ["relaycheck*"]`），pip 用户不受影响；受影响的是「下源码包自己 build」那条路，
而那正是 `gui/README.md` 承诺过的路。本仓库 Release 上的 exe 是用仓库源码构建的，不走 sdist，
所以 Release 里的 0.1.4 是好的。

**测试**：`tests/test_gui.py` 新增 `test_the_sdist_carries_every_file_the_desktop_build_reads`
（19 → 20）—— 读 `MANIFEST.in` 的 `recursive-include gui` 那行，断言 spec 引用到的每个资产
后缀都在里面。验证过「把那行改回只有 `*.md` 即失败」。

## [0.1.4] — 2026-09-27

这一版改的全是**桌面版和构建脚本**，都在 `gui/` 下 —— 而 `gui/` 不进 wheel
（`pyproject.toml` 是 `include = ["relaycheck*"]`）。所以对 `pip install relaycheck` 的人来说，
0.1.4 和 0.1.3 实质只差一个版本号；这版真正变好的是 Release 里那个 exe。
写在这里，免得有人升级完发现什么都没变还以为自己装错了。

### 新增：桌面版结果卡显示「家族线索」（每个模型自己供出的身份）

identity 探针每跑一次都会拿到「这个模型说自己是哪个厂商的」，但这份数据只以一条 finding 的
**标题**（「模型自称的厂商与售卖名称不一致」）出现在窗口里，真正的逐模型自述和原文句子留在
`report.json` 里。于是使用者看到「有问题」三个字，看不到问题长什么样。

现在结果卡底部多一段：

```
家族线索：每个模型自己供出的身份（只是线索，单独一条不足以定性）
  ! deepseek-chat      售卖 deepseek，自称 minimax       「I was developed by MiniMax.」
  ! gemini-1.5-pro     售卖 google，自称 openai          「I was created by OpenAI.」
  = claude-3-5-sonnet  售卖 anthropic，自称 anthropic（一致）
  = gpt-4o             售卖 openai，自称 openai（一致）
```

这样写的理由：**本项目最容易被误读的一句话是「模型自称的厂商不对」**。把原文摊开，读者自己
就能看出这是「I was created by OpenAI」这种套话——官方 DeepSeek 端点也这么答（见 README
「诚实的局限」）。一条能自己验证的免责声明，比一条要求你相信的免责声明值钱。

细节：

- 矛盾（`!`）排最前，一致（`=`）在后 —— 有意思的不能压在下面。
- 引文取自 `id-100` 自己的 `evidence.contradictions[].quote`，**不在窗口里重新挑句子**，
  否则窗口引用的句子可能与报告不一致。
- 交付方式（`unstable` / `unrechecked` / 采集失败）各有自己的说法，不假装拿到了自述。
- 未知 verdict 退化成「没拿到可用的自述」，而不是空行或异常。
- 切卡时清空：连跑两个站再看「运行失败」，不会把上一个站的模型身份挂在这个站下面。
- 对齐是手工算的（模型名按 ASCII 补宽、引文列按「CJK 算两列」的宽度补齐）——Consolas 只覆盖
  ASCII，Tk 会给中文换一个比例字体，不补宽就会阶梯状错位。

**测试**：`tests/test_gui.py` 新增
`test_the_card_lists_what_each_model_said_it_was`（含对齐断言）与
`test_switching_cards_clears_the_previous_runs_family_lines`（12 → 14 项）。
两条都验证过「把修复回退即失败」（`AttributeError: ... no attribute '_family_lines'` /
`TypeError: _show_card() takes 4 positional arguments but 5 were given`）。

### 新增：桌面版的三处可用性修复（图标 / 进度条 / 花钱提醒）

**一、窗口和 exe 有了图标。** 在这之前 `gui/` 里一个图标文件都没有：exe 顶着 PyInstaller
的默认标记，窗口是 Tk 的羽毛。`gui/make_icon.py` 从源码里的几何参数生成 `relaycheck.ico`
（16/24/32/48/64/128/256 七档）和 `relaycheck.png`（Tk 读不了 ico，窗口得用 png），
两个 exe 走 `icon=`，窗口走 `iconphoto`。

设计上只有两条硬约束，写在生成器的 docstring 里：**必须活过 16px** —— 资源管理器的小图标
视图和小任务栏就用这一档，只在 256px 好看的那叫插画；以及**不能被读成又一个安全盾牌**。
最后选的是 `≠`，因为它就是整个主张：你要的和回来的不是同一个。放大镜和双向箭头两版都在
16px 上被淘汰。

这不是纯装饰：PyInstaller 的默认图标本身就会抬高杀软的启发式评分，和 `upx=False` 同一个理由。

窗口图标那次加载**不是一次就成的**。在一个反复建窗口的长命进程里，`image create photo -file`
大约十五次里会有一次抛出一个**消息为空**的 `TclError`（`::errorInfo` 也是空的），紧接着重试就
成功，root 状态完全正常 —— 是单次 Tcl 调用的瞬时失败，不是状态坏了。所以 `_apply_icon` 试两次，
两次都失败才把原因记进 `_icon_error`。一次偶发不该让窗口顶着 Tk 默认羽毛，而真正的失败
（png 读不了、被截断）两次都会失败，照样会被记下来而不是吞掉。

**二、进度条。** 一次标准检测要跑四分钟上下，之前窗口里唯一的生命迹象是日志在滚。现在按钮行
右侧有一条 `ttk.Progressbar`，**数据全部来自 CLI 自己打印的进度行**（`探针: …` / `→ name …` /
`√ name: flag`），窗口不自己数数。条走的是**已完成**的项，状态行说的是**正在跑**的项，两者
刻意不同。

中途「停止」不会把条补满：条停在真实完成数上，状态写「提前结束（3/8 项，退出码 1）」。把没跑完
的检测画成 100%，就是在替使用者下一个他没同意的结论。

**三、花钱提醒换了位置。** 那句「检测会消耗你自己的 API 额度」原本在按钮行的最右边，正文字号，
离它警告的那个按钮隔了大半个窗口。现在它单独一行、紧贴「开始检测」下方、加粗并大一号 ——
全屏唯一一句要花钱的话，不该是最容易被忽略的那句。

**测试**：`tests/test_gui.py` 新增 4 项（14 → 18）：进度条只认 CLI 报过的数、中止的运行不会拿到
满条、窗口真的挂上了生成出来的图标、花钱提醒是粗体且不在按钮行。四条都验证过「把修复回退即失败」
（`AttributeError: ... no attribute '_note_progress'` / `... '_set_progress'` /
`module 'relaycheck_gui' has no attribute '_icon_path'` / `AssertionError: assert 'bold' in ''`）。

### 修正：`gui/build.ps1` 在中文 Windows 上会被拆错行（少了一个 UTF-8 BOM）

这个文件一直没有 BOM。**Windows PowerShell 5.1 对没有 BOM 的 `.ps1` 按 ANSI 解码**，在中文机器
上就是 cp936 —— 而 cp936 有前导字节，中文注释末尾的字节会和后面的换行配成一个双字节字符，
**把下一行源码吞进注释里**。实测这个文件有 16 行是这么没的（154 行被读成 138 行），其中一行是
`$Probe = '...'`，于是：

```
.\build.ps1 -Python 'E:\anaconda\python.exe'
→ python.exe : Argument expected for the -c option
→ 没找到可用解释器。要求 Python >= 3.9 并且带 tkinter。
```

而同一个解释器手动跑得好好的。`-Python` 这个参数从写下来那天起，在中文机器上就没成功过一次。

CI 抓不到：runner 是 en-US，cp1252 没有前导字节，同一个文件在那边解析完全正常。修复是给文件加
BOM（PS 5.1 和 7 都会因此按 UTF-8 读），**源码一个字都没改**。

**测试**：`tests/test_gui.py` 新增 `test_the_build_script_survives_a_chinese_locale_windows_powershell`
（18 → 19）—— 断言 `gui/build.ps1` 带 BOM，且 BOM 之后仍是合法 UTF-8。验证过「把 BOM 去掉即失败」。

## [0.1.3] — 2026-09-26

这一版只做一件事：把审计里唯一一个**判断**（选哪几个模型来测）也写进产物。

### 新增：报告记录受测模型的**来源池**与选型依据

`report.json` 原来只有一个 `models_available_count`（数字）、一个 `models_tested`（受测清单），
以及一条只打到控制台的选型说明。于是一份报告能告诉你「测了 2 个模型」，却看不出这 2 个是从
什么里挑出来的、为什么是这 2 个——而**选型是整场审计里唯一一个判断，不是一个测量**。测量错了
可以回去复核原始数据，判断错了在产物里根本看不见。

现在：

- `report.json` 新增 `models_available`（`/v1/models` 返回的完整清单）与
  `selection: {"mode": ..., "rationale": ...}`；原来的 `models_available_count` 保留不动，
  已有的下游读法不受影响。
- `report.md` 增加「选型」一行与选型依据，并把完整可选池放进一个折叠块。
- 控制台报告增加「选型」「选型依据」两行。

`mode` 为 `auto` 表示模型是按**厂商多样性**挑的（`selection.select_models`：每个声称的厂商
先出一个代表，而不是取列表前 N 个）；`explicit` 表示操作者用 `--models` 直接指定。

**测试**：`tests/test_mock_relay.py` 新增
`test_an_explicit_model_list_still_records_the_pool_it_came_from`（显式指定时同样要记下池子），
并把 `test_model_autodiscovery_runs_without_models_flag` 扩到断言完整池、`selection` 两块、
以及 markdown 里的池子。两条都验证过「把修复回退即失败」（回退后
`KeyError: 'models_available'`）。

## [0.1.2] — 2026-09-26

这一版几乎是单一主题：**不再冤枉诚实站**。最早的几处是把「测不出来」「把厂商认错」
直接写成了结论；中间一批修的是判定条件，它们会把正常端点的形状（只挂一两个模型、没有
计费面板、usage 是 Anthropic 形、官方 API 在探测节奏下限流）当成罪证；第三批修的是
「根本没有测量对象却仍然下结论」的路径——它们不需要中转站做错任何事就能触发。最后一批
反而是反向的：把一处**真实存在、但被推理模型吃掉**的测量抢救回来。

两条验证记录（官方直连端点、一家诚实中转站）都已按本版构建重跑，结论一致：**没有任何
一条 finding 高于 `INFO`，没有一条把「没测出来」写成指控**。原始数字见下面的文档条目。

- 文档：`README.md` / `README.en.md` 新增「验证记录」一节，记录两次真实端点的实测。
  ① 官方直连端点（`https://api.deepseek.com`，`deepseek-flash` + `deepseek-v4-pro`，全量
  11 个探针，219.0 秒 / 129 次请求）：退出码 `0`，`CRITICAL/HIGH/MEDIUM/LOW` 全 `0`，
  INFO 9 条、CLEAN 4 条；9 条 INFO 里 8 条是「未能测量」，剩下 1 条 `id-100` 的标题本身
  就写着「线索，非结论」，没有一条被写成指控。② 一家诚实中转站（转售同一批模型、公开
  sub2api 面板，181.7 秒 / 132 次请求）：同样全 `0`，INFO 11 条、CLEAN 2 条，面板自报
  加价倍率 1.0x，计费金额与上游成本逐项完全相等。
  同一节也写明了这次实测确认的三件事：模型自述无证据价值（官方 `deepseek-flash` 稳定
  自称 ChatGPT/GPT-5，表里那条 `id-100` 就是官方端点**自己**触发的）、`temperature=0` 下
  不可复现属于上游行为、`n`/`tools` 报 400 属于上游策略——后两条正是
  `params-104`/`stream-104`/`params-102` 只能是 INFO 的原因。另把「诚实的局限」第 3、5、9
  条原来引用的匿名中转站例子换成官方端点的可复现证据。
  这两条记录都已按 `max_tokens` 覆盖率修复后的构建重跑：原先两边各有一条
  `params-103 | 2 项样本为空`，现在整条消失（官方端点两个模型都改判 `honoured`，
  证据是 `requested=16 / billed=16 / finish_reason=length`），官方记录里也补上了
  `14 项参数检查跑了 8 项` 的分解。
- 文档：`gui/README.md` 里的桌面包体积改成实测区间（本机 14.2 MB，GitHub CI 构建 15.0 MB）。

- 文档：`README.md` / `README.en.md` 把「模型自述不可靠」那一段的**成因**写准了：模型的
  自我认知来自训练语料，DeepSeek 系列自称 OpenAI 是蒸馏训练留下的口音，与它跑在谁的
  集群上无关；并点明这**恰好是官方端点**的特征，不是异常。

### 修正：`max_tokens` 在推理模型上被整条丢掉（覆盖率缺口，不是误报）

官方直连端点上发现的缺口：`params-103` 记了「2 项样本不可用」，但其中一项本来是**可以
判定的**。`deepseek-flash` 在 `max_tokens=16` 下返回的是
`billed_completion_tokens=16` + `finish_reason="length"` + 空正文——`length` 并且计费数
正好落在我们请求的上限上，这已经是「上限确实被施加」的直接证据：把 `max_tokens` 丢掉的
中转站会让模型答完，并把 `finish_reason` 写成 `stop`。

原代码在 `_check_max_tokens` 里把 `if reasoning:` 的门放在数字判断**之前**，于是任何推理
模型都直接落到 `inconclusive`，这条证据连看都没被看过。现在把「计费数 ≤ 请求的上限
**且** `finish_reason == "length"`」提到 reasoning 门之前。

这一支对非推理模型是 no-op——`billed <= 16` 在旧的 `billed > 24` 判据下本来就得出
`honoured`——所以 `fraudulent` / `clean` / `same-vendor` 三条既有结论逐条不变。`if
reasoning:` 的 bail-out 也照旧保留：推理模型**超出**上限时，`completion_tokens` 把隐藏
reasoning 也算进去，那仍然量不出来，照样不指控。顺带把散落的 `16` 提成常量
`_MAX_TOKENS_REQUEST`。

**测试**：新增 `test_a_reasoning_model_that_stops_at_the_cap_is_credited`。`reasoning`
场景本身就能造出这个形状，因此没有新增第 9 个场景。回退 `params.py` 即失败：
`assert 'inconclusive' == 'honoured'`。

### 修正：四处「没有可观测量，却给出了结论」的地方

第三批。前两批修的是**判定条件**，这一批修的是**根本没有测量对象**时仍然下结论的路径。

- `bill-100` / `bill-clean`：隐藏 reasoning 计费探针原来直接调 `ctx.client.chat`，绕过了
  `ctx.ask` 的 grow 重试——而全项目只有它这么做。于是推理后端把 512 token 预算全烧在隐藏
  reasoning 上时，探针拿到的是一段**空正文**，并且把它当成了一次测量：`billed == 0` →
  `expected == 0` → `billed_vs_estimated_ratio` 是 `None`，同一个记录里却写着
  `verdict: "visible-reasoning-ok"`（「已检查且正常」）。现在改走 `ctx.ask`（512 → 2048
  重试），并且空正文/被截断的样本一律先判 `unmeasurable`，只有真拿到可见回答的模型才可能
  进入 CLEAN；一个都没拿到时发新增的 `bill-000`（INFO，「隐藏 reasoning 计费未能检测」）。
  这恰恰是这类作弊最需要被查的形态，原来却是唯一被顺手放过的那一支。
- `params-100` 的 `stop`：判据是
  `honoured = _STOP_TOKEN not in stopped and len(stopped) < len(plain)`。空回答**天然满足
  这两半**——token 当然不在空串里，`0 < len(plain)` 也成立——于是一个被预算挤空的回答被记成
  「stop 生效」，`params-clean` 接着为一个从未发生过的测量背书。同一处的说明文字还是无条件
  发出的：`honoured` 那一支里它写着「stop 被静默丢弃，与不带 stop 完全一致」，
  结论与说明互相矛盾。现在空回答判 `inconclusive`，说明跟着分支走。
  （只有空串被拦：`_STOP_PROMPT` 要的是 10 个一位数字，诚实的「生效」与「没生效」两种回答
  都远不到 24 字符，用 `starved_of_visible_text` 的门会把这一项本身废掉。）
- `rel-101`：判定顺序原来是「先看中位延迟，再看有没有被回绝」。但被限流的那一轮**仍有成功
  样本**，所以中位数照样存在——它里面混着端点让我们在限流队列里排队的时间。于是
  「上游是共享池、排队严重或者被限速，而不是直连官方 API」这句话会在官方端点上原样发出：
  官方 API 在 0.4 秒一次的探测节奏下限流本来就是常态。现在只要这一轮出现过 429/4xx，
  就不再拿延迟定性；延迟仍然记录，但只作为观察值出现在 `rel-102` 的说明里。

**测试**：新增 4 条回归测试，每条都做过「把修复回退掉就会失败」的验证。新旋钮
`throttle_every=N` 让每第 N 次请求回 429、其余正常服务——这正是 `rel-101` 需要的形状
（有成功样本、也有回绝证据），挂在既有场景上，**场景数仍然是八个**。

### 修正：又一批「把测不出来 / 把厂商认错」写成了结论

第二批，专门冲着**官方直连端点的形状**去的：只挂一两个模型、没有计费面板、usage 是
Anthropic 形。这三件事没有一件是掉包证据，但它们的每一件都会让上一版开口。

**MEDIUM 级（会指控一个正常工作的端点）**

- `tok-001`：一个模型报 `prompt_tokens`、另一个不报，原来就发 MEDIUM。但只说
  Anthropic 形 usage（`input_tokens` / `output_tokens`）的兼容层、以及在流式响应上省略
  usage 的网关，跑的**都是真模型**。现在 `tok-001` 是 INFO，措辞是「这些模型的 tokenizer
  指纹这一项**没有被检验**」——缺 usage 是兼容性缺陷，可以要求站方补齐，但它本身不是掉包
  的证据。（`tok-000` 全部模型都缺那一支不变。）
- `twins-100`：`twins` 的最后一档 `else` 吞掉了所有「既不跨厂商、也没有 tokenizer 命中」
  的模型对。而 `tokenizer_groups` 在单独跑 `--probes twins`、tokenizer 探针失败、或上游
  根本不返回 `prompt_tokens` 时**必然是空的**——于是诚实地只卖一个厂商产品线的中转站，会
  因为一对合法别名收到 MEDIUM，而唯一能替它洗清的那份证据恰好从没被采集。现在这种对子在
  **同厂商**时走 `twins-101`（LOW，并写明「这次没有可用的 tokenizer 指纹作为旁证」）；
  两个名字都识别不出厂商时仍然是 MEDIUM，因为那确实无从判断关系。
- `echo-100`（跨厂商误判）：`claimed_family` 原来把各家族关键词做子串匹配，再用**字典序**
  解歧义。`deepseek-r1-distill-qwen-32b` 同时命中 `deepseek` 和 `qwen`，字典序把它判成
  `alibaba`，于是上游老老实实返回 `deepseek-r1` 反而成了 `cross_vendor`。现在改成**最长
  命中关键词**优先（`deepseek` 长 8 胜 `qwen` 长 4），只有真平局（`openai` 与 `claude`
  都是 6）才回退字典序——保证两次审计同一个目录得到同样的结论。

**不是错误，但会被读成「工具坏了」**

- 单模型端点：`twins` 原来在模型不足两个时写 `result.error = "需要至少 2 个模型…"`。报告层
  把任何 `result.error` 渲染成「探针崩溃」，命令行把它算作探针失败——`api.deepseek.com`
  这种当下只挂一两个模型的官方端点，看起来就像 relaycheck 自己炸了。现在发 `twins-000`
  （INFO，「双胞胎比对未能进行」），把「这一项没做」和「这一项做了、没问题」彻底分开。

**测试**：新增 4 条回归测试（`tests/test_mock_relay.py` 3 条 + `tests/test_selection.py`
1 条），每条都做过「把修复回退掉就会失败」的验证；同时把 3 条此前只存在于 pytest 收集里、
没登记进 `_main()` 自跑清单的用例补上。新旋钮 `strip_usage_for=("模型名",)` 挂在既有场景
上——**场景数仍然是八个**。

### 修正：把「测不出来」当成「测出问题」的一批路径

拿官方直连端点当金标准重读了一遍探针代码，找出十几处**会向诚实端点发警报**的地方。
它们属于同一类错误：一个本该是「无法判定」的结果被写成了结论。修法一律是补三态门，
不是调阈值。

**HIGH 级（会直接指控一个正常工作的端点）**

- `rel-100`：`failure_rate` 原来不分状态码，官方端点在 `--delay 0.4` 的连发节奏下回的
  **429 限流**被算成「端点撑不住」。现在 429 和 4xx 单独成桶，只有 5xx / 超时 / 连接
  失败才计入不可用率；有回绝但没有不可用时走新增的 `rel-102`（INFO，
  「可用性没能测出来：N/M 次被端点主动回绝」）。
- `canary-100`：标记没回显就报 HIGH，但被**我们自己设的** `max_tokens` 挤空
  （`finish_reason=length`）同样落进这一支。现在这种尝试直接不进证据；全部尝试都被挤
  空时降级为 `canary-000`（INFO，并把原因记成 `unmeasured` 而不是「没尝试」）。
- `stream-100`：`answered` 原来只查「`1517` 这个子串出现过」——`"1517"` 和
  `"37 * 41 = 1517"` 都算命中，配上 90% 前缀容差的文本比对就变成「两条路径答案不同」。
  现在要求两侧**都给出**参考答案才算一致；要判不一致，还需要两边报出的数字**完全不相交**。
- `stream-100`：可复现性原来借 `_compare_answers` 判定，而那个函数为了容忍被截断的流，
  把「前缀相同、长度差 10% 以内」也算 match。现在 `_reproduces_itself` 要求逐字相同。
- `stream-100`：两条回复都被 `max_tokens` 截断时不再比较——截断点不同只说明隐藏
  reasoning 的消耗不同，不说明中转站换了后端。
- `ctx-100`：长上下文模型最常见的礼貌回绝是「我只看到一个标记：…」，正文里出现的另一个
  标记是**引用**。原来只要标记出现在正文任意位置就算「看到了」，于是把回绝读成「前段
  输入在到达模型之前就被丢掉了」。现在带否定上下文时该档不判读（verdict `denied`）。

**CLEAN 级（把「什么都没测到」写成「已验证没问题」）**

- `bill-clean`：`reachable` 原来是「响应里没有 `error` 键」，而 404 是**正常返回**的
  dict，于是六个面板端点全部 404 也报「6 个可达」，`bill-202`（面板全部不可达）实际
  是死代码。现在要求 2xx + 可解析的 JSON 对象（`{"_raw_text": ...}` 不算），并把
  `/health` 单独排除——它在任何部署上都回 200，不能拿来给计费下结论。
- `id-clean`：空回答被算成「可用自述」（`"".startswith("<")` 是 False），模型被挤空时
  照样报 CLEAN。现在要求 `v.strip()`。
- `params-clean`：`matrix` 为空（检查被过滤掉、或预算在第一档就耗尽）时也会发。现在走
  新增的 `params-105`（INFO，「参数检查一项都没跑完」）。
- 隐藏 reasoning 计费那一支的空正文路径原来写 `verdict="ok"`，措辞与实际不符。

**MEDIUM 级（参数检查把模型的自愿行为当成违约）**

- `tools`：请求用 `tool_choice="auto"`，而 `auto` 的语义就是「模型可以不调用」，于是官方
  端点只要用散文回答就被判「参数被静默丢弃」。现在改用 `tool_choice="required"`
  （服务端承诺必须回 tool_call，此时散文回复才算承诺没兑现）。
- `logprobs` / `n` / `tools`：补 `_has_choices` 门，非 JSON 的 200 正文（被
  `RelayClient` 包成 `{"_raw_text": ...}`，能骗过裸 `isinstance(body, dict)`）不再落进
  「被忽略」。
- `max_tokens`：`completion_tokens` 在 DeepSeek / OpenAI 兼容的推理端点上**含**隐藏
  reasoning token，而 `max_tokens` 只封可见正文。现在 reasoning token 非 0 时判
  inconclusive。
- `response_format`：接受 ```json 围栏。
- `stream-101`：只有流里**完全没有** usage 块才算缺失。兼容层返回 Anthropic 形 usage
  （`input_tokens` / `output_tokens`，没有 `total_tokens`）不再算「未返回 usage」。
- `stream-102`：突发判定改用首字延迟之后的时间窗计算。推理端点先想 20–40 秒再两秒流完，
  按整条请求的墙钟算必然落进「一次性下发」。

**测试**：`tests/test_mock_relay.py` 从 15 项加到 21 项，新增的每一条都做过「把修复回退
掉就会失败」的验证。新旋钮（`panel=False`、`reasoning_tokens=`、`throttle=True`）都挂在
既有场景上——**场景数仍然是八个**。

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
