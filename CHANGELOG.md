# 变更记录

本文件记录**行为**的变化，尤其是「严重程度」和「finding id」的语义变化——
一个已经发布过的 finding id 尽量不重用；如果它的含义变了，这里必须写明。

## [0.1.0] — 未发布

首个版本。11 个探针，8 个本地模拟场景，14 项验收。

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

### 验收（`tests/test_mock_relay.py`，14 项）

- `fraudulent` 必须被抓出来：≥1 条 `CRITICAL`，且是具体的 finding id。
- `clean` / `same-vendor` / `slow` / `dead` / `reasoning` / `noisy` / `unstable-self`
  必须**零误报**（`same-vendor` 允许一条 `LOW` 别名提示）。
- `dead` 永远不能被报成 `CLEAN`。
- CLI 在 cp936 控制台下必须正常收尾并退出 `0`。
