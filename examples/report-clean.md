# relaycheck 审计报告

| 项目 | 值 |
| --- | --- |
| 目标 | `http://127.0.0.1:8201` |
| 时间 | 2026-09-26 00:08:23 +0800 |
| 耗时 | 0.9s / 124 次请求 |
| 工具版本 | 0.1.0 |
| 探针 | reliability, billing-panel, echo, tokenizer, twins, identity, canary, billing-hidden-reasoning, params, stream, context |
| 可用模型数 | 3 |
| 受测模型 | `gpt-4o`, `claude-3-5-sonnet`, `deepseek-chat` |

## 结论

**未检测到问题**

| 严重程度 | 数量 |
| --- | --- |
| CRITICAL | 0 |
| HIGH | 0 |
| MEDIUM | 0 |
| LOW | 0 |
| INFO | 2 |
| CLEAN | 10 |

## 发现

### [INFO] 可用性正常

- **编号**：`rel-clean`
- **可信度**：已确认

最简请求 8/8 成功，中位延迟 0.00 秒。

<details><summary>证据</summary>

```json
{
  "model": "gpt-4o",
  "samples": 8,
  "succeeded": 8,
  "failed": 0,
  "failure_rate": 0.0,
  "timeout_s": 30.0,
  "retries_disabled": true,
  "p50_latency_s": 0.0,
  "min_latency_s": 0.0,
  "max_latency_s": 0.016,
  "mean_latency_s": 0.004
}
```

</details>

### [INFO] 识别为 sub2api 面板，并读到其自称的计费口径

- **编号**：`bill-200`
- **可信度**：已确认

该中转站运行 sub2api 面板，接口自报 billing_scope='token'、effective_rate_multiplier=0.32。这是面板配置的权威值——如果商家的口头说法与它矛盾，以这里为准。

> **建议**：将该值与商家的计费说明逐条对照；billing_scope=token 时，任何『按请求数计费』的说法都是假的。

<details><summary>证据</summary>

```json
{
  "billing_scope": "token",
  "rate_multiplier": 0.32,
  "raw": {
    "object": "sub2api.key_billing",
    "schema_version": 1,
    "billing_scope": "token",
    "group_rate_multiplier": 0.32,
    "resolved_rate_multiplier": 0.32,
    "peak_rate_enabled": false,
    "effective_rate_multiplier": 0.32
  }
}
```

</details>

## 已核实正常

- `bill-clean` 面板可达，但未暴露上游成本
- `echo-clean` 响应体自报的模型名与请求一致
- `tok-clean` 未发现 tokenizer 相同的模型对
- `twins-clean` 未发现行为上完全一致的模型对
- `id-clean` 未发现模型自述与售卖名称矛盾
- `canary-clean` 标记回显正常
- `bill-clean` 未发现隐藏 reasoning 计费
- `params-clean` 参数透传正常
- `stream-clean` 流式与完整返回内容一致
- `ctx-clean` 长输入在测试范围内完整到达模型

## 原始数据

完整原始返回见同目录 JSON 报告，可自行复核任何结论。
