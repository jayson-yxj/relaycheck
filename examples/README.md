# 示例报告

这四份报告都是 `relaycheck` 的真实输出，不是手写的。它们跑在仓库自带的
`tests/mock_relay.py` 假中转站上，因此内容可复现、也不含任何真实服务的数据。

| 文件 | 场景 | 结论 | 用途 |
| --- | --- | --- | --- |
| `report-fraudulent.md` | 掉包站：4 个售卖名称背后只有 2 个真实后端，且跨厂商混用；响应体自报的模型名与请求不符；计费按更贵的上游模型计；忽略 7 个控制参数；长输入被砍到只剩最后 2 万个字符 | 2 CRITICAL / 4 HIGH / 6 MEDIUM / 3 INFO | 看「有问题」长什么样 |
| `report-clean.md` | 诚实站：3 个售卖名称对应 3 个真实后端，响应体自报的模型名与请求一致，参数透传正常，流式与完整返回一致，长输入完整转发 | 0 个 CLEAN 以上的发现 | 看「没问题」长什么样 |
| `report-dead.md` | 死站：面板和模型列表正常返回，但每一次 completion 都失败 | 1 HIGH / 10 INFO / 1 CLEAN | 看「**查不成**」长什么样 |
| `report-noisy.md` | 诚实但**输出不可复现**的站：同一个请求在 `temperature=0` 下连发两次会得到两段不同的文字，别的都正常 | 5 INFO / 7 CLEAN | 看「**查不实**」长什么样 |

四份报告用的是同一套探针、同一份代码，差别只在被审计的服务。这一点很重要：

**一个只会报「有问题」的工具没有信息量。** 如果 `relaycheck` 在诚实站上也会刷出一堆
发现，那它报告的「掉包」就不可信。所以 `report-clean.md` 和 `report-fraudulent.md`
是同等重要的产物 —— 前者是后者的对照组。

`report-dead.md` 是第三种对照组，守的是另一条底线：**`CLEAN` 必须意味着「已经验证过」。**
一份「全绿」的报告比没有报告更糟 —— 它会把一次真实的掉包洗成清白证明。
所以在死站上，十一个探针里只有面板探针能给出 `CLEAN`（`bill-clean`，因为它确实读到了面板
并确认面板没暴露上游成本）。其余探针一律不发 `CLEAN`：九项报 `INFO` 的 `*-000` / `*-102`
（含 `echo-000`「未能完成模型自报名的对照」）并写明「这一项没有被检验」，而可用性探针直接报
HIGH 的 `rel-100` —— 那不是指控掉包，而是指控**可用性**，也是死站上少数仍然能得出结论的检查。

`report-noisy.md` 是第四种对照组，守的是第三种混淆：**「测出来的差异」不等于「测出来的问题」。**
这个站在 `temperature=0` 下都做不到可复现 —— 上游是 MoE 或者批处理后端时这是正常现象，
不是作弊。于是「流式回答和完整回答不一样」和「两次相同调用结果不一样」这两件事，
都只是**噪声本来的样子**。`report-noisy.md` 里可以看到报告怎么处理它：`stream-104` 和
`params-104` 两条 INFO 明说「这一项无法判定」，正文里各自写清「**本条不指控**参数被忽略」，
而不是把噪声写成 HIGH。反过来，如果探针在这里干脆闭嘴，读者会以为两条路径已经比对过了 ——
沉默同样是撒谎。

四份报告里都能看到 `echo` 探针的输出，它是全套里最便宜的一条硬证据（每个模型两次请求）：
`report-fraudulent.md` 是 `echo-100`（响应体自称 `gpt-4o`，而请求的是 `gemini-1.5-pro`，
连厂商都对不上），`report-clean.md` / `report-noisy.md` 是 `echo-clean`，
`report-dead.md` 是 `echo-000`（请求全失败，这一项没测成 —— 不是「没问题」）。
它的判据只有一句话：**响应体里的 `model` 字段，是不是你下单的那个名字。**
这个字段由上游填写、站方通常不改写，所以它对不上时值得记录；但站方也可能套了一层自己的
第三方命名，所以「同厂商的另一个名字」只记 INFO（`echo-101`），不构成指控。

四份报告都包含 `context` 探针的输出，可以看到它三种结局的差别：掉包站是
`ctx-100`（HIGH，有更浅的对照深度），诚实站是 `ctx-clean`，死站是 `ctx-000`（INFO，未检验）。

## 怎么自己复现

```bash
# 终端 1：起假中转站
python tests/mock_relay.py --port 8200 --scenario fraudulent

# 终端 2：审计它
python -m relaycheck.cli \
  -u http://127.0.0.1:8200 \
  -k sk-relaycheck-local-test \
  --models "gpt-4o,gemini-1.5-pro,claude-3-5-sonnet,deepseek-chat" \
  --probes all --delay 0

# 诚实站（模型名必须是 mock 里真实的三个，否则会得到一串 404 噪音）
python tests/mock_relay.py --port 8201 --scenario clean
python -m relaycheck.cli \
  -u http://127.0.0.1:8201 \
  -k sk-relaycheck-local-test \
  --models "gpt-4o,claude-3-5-sonnet,deepseek-chat" \
  --probes all --delay 0

# 不可复现的诚实站
python tests/mock_relay.py --port 8203 --scenario noisy
python -m relaycheck.cli \
  -u http://127.0.0.1:8203 \
  -k sk-relaycheck-local-test \
  --models "gpt-4o,claude-3-5-sonnet,deepseek-chat" \
  --probes all --delay 0
```

`--scenario slow` 还提供一个「行为完全正确、只是很慢」的站点，用来验证
`--budget` 不会让审计挂死，同时也不会把「慢」误报成「有问题」。

死站示例的复现命令（`--timeout 10` 是为了别等太久；真实站点上请用默认值）：

```bash
python tests/mock_relay.py --port 8202 --scenario dead
python -m relaycheck.cli \
  -u http://127.0.0.1:8202 \
  -k sk-relaycheck-local-test \
  --models "gpt-4o,claude-3-5-sonnet,deepseek-chat" \
  --probes all --delay 0 --timeout 10
```

复现时注意：**报告里的时间、耗时会变，finding 的 id 和数量不应变。** 如果某个探针在
死站上给出了 `*-clean`，那就是 bug —— `tests/test_mock_relay.py` 里的
`test_dead_relay_is_never_reported_as_clean` 会当场把它抓出来。同理，如果探针在
`noisy` 站上报出 `stream-100` 或 `params-100`，`test_noisy_relay_is_not_falsely_accused`
会当场抓住它。
