# 参与 relaycheck

这个项目最缺的**不是功能，是反例**。

`relaycheck` 的定位是一句话：**当审计工具能用，当判决书不能用。** 它所有发现只到
**厂商家族（family）级别**，从不断言「你被换成了某个具体模型」。这个定位能不能站住，
取决于一件事：**它不许对诚实的站乱叫。** 一个爱叫的工具会把一次真实的指控洗成噪音，
比没有工具更糟。

所以下面两类 issue 比任何 feature request 都值钱：

| | 意思 | 标签 |
| --- | --- | --- |
| **误报** | 对一家诚实的站报了 MEDIUM 及以上 | `false-positive` |
| **漏报** | 一家明显在掉包的站，`relaycheck` 说它没问题 | `missed-detection` |

「慢」不算问题，「贵」不算问题，`LOW` / `INFO` 也不算误报 —— 阈值见下。

---

## 报一条误报

**必须带的三样：**

1. **`report.json`**（`--out-dir` 目录里那份）。`report.md` 不够：它把证据压成了一行，
   复现要用结构化字段。
2. **哪一条 finding 不该报。** 用 id（`twins-100` / `ctx-100` / `bill-201` …），
   别说「它报错了」。
3. **为什么不该报。** 这条最重要，也最常缺。有效的「为什么」长这样：

   - 「这两个名字本来就该共享 tokenizer，因为对方文档里写了别名关系」；
   - 「我拿官方端点在同样参数下试了，也是这个行为」（附上官方那次的 `report.json`）；
   - 「这条 finding 的证据字段里引用的两个样本其实都是空字符串」。

   **无效的「为什么」**：「我在这家充了钱，它不可能骗我。」

### 先把你的账单删掉

报告里的余额、消费、用量来自 `billing` 探针（`results[].probe == "billing"`），
那是**你的**消费记录。一条命令摘掉它：

```bash
python -c "import json,pathlib;p=pathlib.Path('out/report.json');d=json.loads(p.read_text(encoding='utf-8'));d['results']=[r for r in d['results'] if r.get('probe')!='billing'];p.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')"
```

**Key 不用管 —— 报告里本来就没有。** `relaycheck/reporter.py` 全文不引用 `api_key`，
只写目标 URL；Authorization 头和 cookie 也不进产物。这条可以自己验：

```bash
grep -ri "api_key" relaycheck/reporter.py     # 无输出
```

---

## 报一条漏报

**先确认它真的是漏报，而不是「我们没测」。** 这是这个项目最核心的不变式：

> `CLEAN` 必须意味着**已经验证过**。
> 「对方没回答」永远不能是 `CLEAN`；「我们没量到」永远不能变成「我们量出了问题」。

所以在报漏报之前，先在 `report.json` 里看两件事：

- 那条探针是不是**根本没跑**（`probes` 里没有它）？那就先 `--probes all` 再来。
- 探针是不是报了 `*-000`（没查成）或 `truncated`？那它**没有**说这家站干净。

真的漏报要带：对方的**掉包手法**（你怎么发现的）+ `report.json` +
**「哪条探针本该抓到它却没抓到」**。最后这一条是最有价值的贡献 —— 它直接告诉我们要加什么检测。

---

## 不需要花自己的额度

**请不要为了让 issue 更有说服力去充值。** 两种本地复现方式，都不联网、不花钱：

```bash
# 方式一：起一个预置的假中转站
python tests/mock_relay.py --port 8123 --scenario fraudulent --verbose
# 另开一个终端
python -m relaycheck.cli -u http://127.0.0.1:8123 -k sk-relaycheck-local-test --probes all

# 方式二：直接跑端到端验收，它自己起服务、自己比对
python tests/test_mock_relay.py
```

`tests/mock_relay.py` 的八个场景（`fraudulent` / `clean` / `same-vendor` / `slow` /
`dead` / `reasoning` / `noisy` / `unstable-self`）各自守着一类**具体的误报**，
README 的「开发」一节逐个解释了它们守的是什么。
**如果你的改动让任何一个场景挂了，那不是测试太严，是改动错了。**

---

## 加一条新探针

可以，但先读 `README.md` 的「开发」一节，尤其是这几条：

- **只读。** 只发 `POST /v1/chat/completions`、`GET /v1/models` 这类读请求；
  不改对方配置、不碰计费写接口、不创建账号。
- **便宜，而且在预算内会自己停。** 循环里要检查 `ctx.out_of_budget()`、**单独**捕获
  `RelayBudgetExceeded` 并调 `self._note_budget(...)`。掉进泛化的 `except Exception`
  会把「我们主动停了」变成「中转站报错了」—— 那是**我们自己的超时制造的假指控**。
- **自证。** 证据要能让人不看你的代码就判断你对不对。
- **severity 和 confidence 分开。** `CRITICAL` / `HIGH` 意味着结果只能这么解释；
  有线索但撑不起指控的，写 `LOW` / `INFO` / `SUSPECTED`。
- **新发现用新 id，已发布的 id 不复用**（语义变更写进 `CHANGELOG.md`）。
- **默认不进** `DEFAULT_PROBE_NAMES`，除非它便宜到可以每次都跑。贵的
  （比如 `context`，一次 32k token 请求可能和其余所有探针加起来一样贵）留在
  `HEAVY_PROBE_NAMES` 里，靠 `--probes all` 显式打开。

---

## 环境与约定

- Python **≥ 3.9**（CI 跑 3.9 / 3.11 / 3.13）。
- 运行时依赖只有 `requests`，其余全用标准库。**不要为了一个小问题引入新依赖。**
- 测试**不能依赖 Pillow**：CI 只装 `requests` + `pytest`。`gui/make_icon.py` 是本机工具，
  不在测试路径上。
- 所有面向人的输出必须能活过 **cp936 控制台**（中文区 Windows 的默认代码页）。
  写 stdout 之前先让 CLI 的 `force_utf8_output()` 生效；有回归测试守着这一条。
- `gui/` **不进 wheel**，但**进 sdist**。改 `gui/` 时记得 `MANIFEST.in` 里那条
  `recursive-include gui`：0.1.4 就是因为漏了 `*.ico *.png`，让整个桌面构建直接挂掉的。
- 提交信息用中文或英文都行，但要说清**行为变了什么**（finding id、严重度、退出码
  这三类变化必须同时写进 `CHANGELOG.md`）。

---

## 提交前

```bash
python tests/test_selection.py     # 16 项，秒级，不联网
python tests/test_mock_relay.py    # 29 项，端到端，会起本地服务
python tests/test_gui.py           # 33 项，没有 tkinter 时自动跳过
```

`test_mock_relay.py` 里的 `dead` 场景单跑约 250 秒，想快可以：

```bash
python -m pytest -q -k "not dead and not slow"
```

**不要用 `--deselect`** —— 它在 Windows 上不生效（跑了等于没跑，看起来还是绿的）。

---

## 许可

贡献即同意以 [MIT](LICENSE) 授权。
