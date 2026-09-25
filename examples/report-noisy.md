# relaycheck 审计报告

| 项目 | 值 |
| --- | --- |
| 目标 | `http://127.0.0.1:8203` |
| 时间 | 2026-09-26 00:13:14 +0800 |
| 耗时 | 1.2s / 130 次请求 |
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
| INFO | 5 |
| CLEAN | 7 |

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

### [INFO] 双胞胎比对未能进行

- **编号**：`twins-000`
- **可信度**：已确认

只有 0/3 个模型产出了可复现的输出（至少需要 2 个才能互相比对）。这一项没有被检验。

<details><summary>证据</summary>

```json
{
  "models_with_output": [],
  "models_requested": [
    "gpt-4o",
    "claude-3-5-sonnet",
    "deepseek-chat"
  ],
  "failures": {},
  "unstable_prompts": {
    "gpt-4o": [
      "protocol",
      "haiku",
      "colors",
      "city"
    ],
    "claude-3-5-sonnet": [
      "protocol",
      "haiku",
      "colors",
      "city"
    ],
    "deepseek-chat": [
      "protocol",
      "haiku",
      "colors",
      "city"
    ]
  },
  "empty_prompts": {}
}
```

</details>

### [INFO] 该站输出不可复现，无法验证采样参数

- **编号**：`params-104`
- **可信度**：已确认

有 2 项采样参数检查中，temperature=0 的两次相同请求返回了不同内容。这可能是中转站丢弃了 temperature，也可能是上游后端本身非确定性（MoE 路由、批处理等），本探针无法区分——因此**本条不指控参数被忽略**。

> **建议**：客户端侧对同一提示词连发多次、比较输出是否一致即可自行确认；若你需要可复现的输出（评测、回归测试），该站不适合。

<details><summary>证据</summary>

```json
{
  "nondeterministic": [
    {
      "model": "gpt-4o",
      "param": "temperature",
      "verdict": "nondeterministic",
      "temp0_runs_identical": false,
      "temp1_5_runs_differ": true,
      "samples": {
        "temp0_a": "gpt-4o says: Write one original sentence of 15 to 30  (draft 420895)",
        "temp0_b": "gpt-4o says: Write one original sentence of 15 to 30  (draft 174209)",
        "temp1_5_a": "gpt-4o draws 7634022: the harbour is built from compacted wind and everyone there speaks in tides.",
        "temp1_5_b": "gpt-4o draws 6177309: the harbour is built from compacted wind and everyone there speaks in tides."
      },
      "note": "temperature=0 的两次调用结果不一致，即该站本身不提供可复现输出。这可能来自中转站丢弃 temperature，也可能来自上游后端本身的非确定性（MoE 路由、批处理等），本探针无法区分。**注意：这不是「参数被忽略」的证据。**"
    },
    {
      "model": "claude-3-5-sonnet",
      "param": "temperature",
      "verdict": "nondeterministic",
      "temp0_runs_identical": false,
      "temp1_5_runs_differ": true,
      "samples": {
        "temp0_a": "claude-3-5-sonnet says: Write one original sentence of 15 to 30  (draft 324813)",
        "temp0_b": "claude-3-5-sonnet says: Write one original sentence of 15 to 30  (draft 524403)",
        "temp1_5_a": "claude-3-5-sonnet draws 6184440: the harbour is built from compacted wind and everyone there speaks in tides.",
        "temp1_5_b": "claude-3-5-sonnet draws 6737852: the harbour is built from compacted wind and everyone there speaks in tides."
      },
      "note": "temperature=0 的两次调用结果不一致，即该站本身不提供可复现输出。这可能来自中转站丢弃 temperature，也可能来自上游后端本身的非确定性（MoE 路由、批处理等），本探针无法区分。**注意：这不是「参数被忽略」的证据。**"
    }
  ]
}
```

</details>

### [INFO] 该站输出不可复现，流式比对无法判定

- **编号**：`stream-104`
- **可信度**：已确认

有 3 个模型在两次完全相同的请求（temperature=0、同一提示词）下返回了不同的内容，即该站本身不提供可复现的输出。这种情况下「流式与完整回答不同」是采样噪声的预期表现，不能作为掉包证据，因此本探针没有据此指控。已改用确定性问答回退，其中 3 个模型的两条路径给出了同一个正确答案，即未发现两条路径的实质分歧；但这一项只覆盖一个问题，强度低于开放式比对。

> **建议**：该站大概率没有把 temperature=0 透传到上游，或上游后端本身非确定性。两种原因本探针无法区分；如需判定，请在客户端对比同一提示词的多次输出。

<details><summary>证据</summary>

```json
{
  "unverifiable": [
    {
      "model": "gpt-4o",
      "reason": "两次相同请求（temperature=0）返回不一致，开放式比对不可用；确定性问答回退的两条路径一致",
      "open_ended_baseline": "mismatch",
      "plain_sample": "PORT=22 ssh\nPORT=53 domain\nPORT=80 http\nPORT=443 https\nPORT=3306 mysql (draft 895483)",
      "control_sample": "PORT=22 ssh\nPORT=53 domain\nPORT=80 http\nPORT=443 https\nPORT=3306 mysql (draft 166050)",
      "fallback": {
        "prompt": "What is 37 * 41? Reply with the number only.",
        "streamed_sample": "1517",
        "plain_sample": "1517",
        "verdict": "match",
        "reference_answer_present": true
      }
    },
    {
      "model": "claude-3-5-sonnet",
      "reason": "两次相同请求（temperature=0）返回不一致，开放式比对不可用；确定性问答回退的两条路径一致",
      "open_ended_baseline": "mismatch",
      "plain_sample": "PORT=22 ssh\nPORT=53 domain\nPORT=80 http\nPORT=443 https\nPORT=3306 mysql (draft 442126)",
      "control_sample": "PORT=22 ssh\nPORT=53 domain\nPORT=80 http\nPORT=443 https\nPORT=3306 mysql (draft 965369)",
      "fallback": {
        "prompt": "What is 37 * 41? Reply with the number only.",
        "streamed_sample": "1517",
        "plain_sample": "1517",
        "verdict": "match",
        "reference_answer_present": true
      }
    },
    {
      "model": "deepseek-chat",
      "reason": "两次相同请求（temperature=0）返回不一致，开放式比对不可用；确定性问答回退的两条路径一致",
      "open_ended_baseline": "mismatch",
      "plain_sample": "PORT=22 ssh\nPORT=53 domain\nPORT=80 http\nPORT=443 https\nPORT=3306 mysql (draft 514989)",
      "control_sample": "PORT=22 ssh\nPORT=53 domain\nPORT=80 http\nPORT=443 https\nPORT=3306 mysql (draft 689470)",
      "fallback": {
        "prompt": "What is 37 * 41? Reply with the number only.",
        "streamed_sample": "1517",
        "plain_sample": "1517",
        "verdict": "match",
        "reference_answer_present": true
      }
    }
  ],
  "models_compared": 0,
  "observations": {
    "gpt-4o": {
      "chunks": 10,
      "ttft_s": 0.047,
      "total_s": 0.172,
      "streamed_chars": 85,
      "plain_chars": 85,
      "control_chars": 85,
      "baseline": "mismatch",
      "usage_in_stream": {
        "prompt_tokens": 26,
        "completion_tokens": 24,
        "total_tokens": 50,
        "cached_tokens": null,
        "cache_read_tokens": null,
        "cache_write_tokens": null,
        "reasoning_tokens": null
      },
      "model_returned": "gpt-4o",
      "finish_reason": "stop",
      "plain_finish_reason": "stop",
      "basis": "deterministic-fallback",
      "verdict": "noisy_but_agreed",
      "fallback": {
        "prompt": "What is 37 * 41? Reply with the number only.",
        "streamed_sample": "1517",
        "plain_sample": "1517",
        "verdict": "match",
        "reference_answer_present": true
      }
    },
    "claude-3-5-sonnet": {
      "chunks": 10,
      "ttft_s": 0.047,
      "total_s": 0.172,
      "streamed_chars": 85,
      "plain_chars": 85,
      "control_chars": 85,
      "baseline": "mismatch",
      "usage_in_stream": {
        "prompt_tokens": 48,
        "completion_tokens": 48,
        "total_tokens": 96,
        "cached_tokens": null,
        "cache_read_tokens": null,
        "cache_write_tokens": null,
        "reasoning_tokens": null
      },
      "model_returned": "claude-3-5-sonnet",
      "finish_reason": "stop",
      "plain_finish_reason": "stop",
      "basis": "deterministic-fallback",
      "verdict": "noisy_but_agreed",
      "fallback": {
        "prompt": "What is 37 * 41? Reply with the number only.",
        "streamed_sample": "1517",
        "plain_sample": "1517",
        "verdict": "match",
        "reference_answer_present": true
      }
    },
    "deepseek-chat": {
      "chunks": 10,
      "ttft_s": 0.047,
      "total_s": 0.172,
      "streamed_chars": 85,
      "plain_chars": 85,
      "control_chars": 85,
      "baseline": "mismatch",
      "usage_in_stream": {
        "prompt_tokens": 26,
        "completion_tokens": 24,
        "total_tokens": 50,
        "cached_tokens": null,
        "cache_read_tokens": null,
        "cache_write_tokens": null,
        "reasoning_tokens": null
      },
      "model_returned": "deepseek-chat",
      "finish_reason": "stop",
      "plain_finish_reason": "stop",
      "basis": "deterministic-fallback",
      "verdict": "noisy_but_agreed",
      "fallback": {
        "prompt": "What is 37 * 41? Reply with the number only.",
        "streamed_sample": "1517",
        "plain_sample": "1517",
        "verdict": "match",
        "reference_answer_present": true
      }
    }
  }
}
```

</details>

## 已核实正常

- `bill-clean` 面板可达，但未暴露上游成本
- `echo-clean` 响应体自报的模型名与请求一致
- `tok-clean` 未发现 tokenizer 相同的模型对
- `id-clean` 未发现模型自述与售卖名称矛盾
- `canary-clean` 标记回显正常
- `bill-clean` 未发现隐藏 reasoning 计费
- `ctx-clean` 长输入在测试范围内完整到达模型

## 原始数据

完整原始返回见同目录 JSON 报告，可自行复核任何结论。
