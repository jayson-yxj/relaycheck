# relaycheck 审计报告

| 项目 | 值 |
| --- | --- |
| 目标 | `http://127.0.0.1:8202` |
| 时间 | 2026-09-26 00:26:02 +0800 |
| 耗时 | 288.8s / 207 次请求 |
| 工具版本 | 0.1.0 |
| 探针 | reliability, billing-panel, echo, tokenizer, twins, identity, canary, billing-hidden-reasoning, params, stream, context |
| 可用模型数 | 3 |
| 受测模型 | `gpt-4o`, `claude-3-5-sonnet`, `deepseek-chat` |

> 备注：中转站可用性很差（失败率 100%），其余探针的结论可能不完整；报告中已逐项标注。

## 结论

**检测到高风险问题**

| 严重程度 | 数量 |
| --- | --- |
| CRITICAL | 0 |
| HIGH | 1 |
| MEDIUM | 0 |
| LOW | 0 |
| INFO | 10 |
| CLEAN | 1 |

## 发现

### [HIGH] 中转站极不稳定：8/8 次极简请求失败

- **编号**：`rel-100`
- **可信度**：已确认

用最简请求（要求只回一个词、上限 8 token）连发 8 次，8 次都返回了错误响应（最典型的是 HTTP 503: chat/completions returned 503 — {"error": {"message": "upstream tempor），没有一次正常完成，失败率 100%。而且这是**关闭重试**测出来的原始成功率。一个连这种请求都服务不好的中转站，既不能稳定给你干活，也让下面所有探针的结论都可能不完整——先解决可用性，再谈掉包。

> **建议**：先看 evidence.failures 里的 error 字段：若是超时，问服务方上游是否限流/超卖；若是 4xx/5xx，那通常是它自己的渠道配置坏了，要求其给出上游原始报错。在稳定性恢复前，不要用它跑批量任务；本次审计报告中标注为「不完整」的项目，都需要在稳定期重跑。

<details><summary>证据</summary>

```json
{
  "model": "gpt-4o",
  "samples": 8,
  "succeeded": 0,
  "failed": 8,
  "failure_rate": 1.0,
  "timeout_s": 10.0,
  "retries_disabled": true,
  "errors": [
    "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
  ],
  "failures": [
    {
      "index": 0,
      "latency_s": 0.0,
      "http_status": 503,
      "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
    },
    {
      "index": 1,
      "latency_s": 0.0,
      "http_status": 503,
      "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
    },
    {
      "index": 2,
      "latency_s": 0.0,
      "http_status": 503,
      "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
    },
    {
      "index": 3,
      "latency_s": 0.016,
      "http_status": 503,
      "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
    },
    {
      "index": 4,
      "latency_s": 0.0,
      "http_status": 503,
      "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
    },
    {
      "index": 5,
      "latency_s": 0.0,
      "http_status": 503,
      "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
    },
    {
      "index": 6,
      "latency_s": 0.0,
      "http_status": 503,
      "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
    },
    {
      "index": 7,
      "latency_s": 0.0,
      "http_status": 503,
      "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
    }
  ],
  "timeout_failures": 0,
  "error_response_failures": 8
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

### [INFO] 未能完成模型自报名的对照

- **编号**：`echo-000`
- **可信度**：已确认

3 个模型里只有 0 个拿到了可用于对照的响应（0/6 处请求成功），另有 6 处请求直接失败。这一项没有被完整检验，它的结论是「不知道」，不是「没问题」。

<details><summary>证据</summary>

```json
{
  "observations": {
    "gpt-4o": {
      "model_requested": "gpt-4o",
      "expected_family": "openai",
      "paths": {
        "plain": {
          "path": "plain",
          "model_returned": null,
          "claimed_family": null,
          "verdict": "error",
          "http_status": 503,
          "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
          "model_requested": "gpt-4o",
          "expected_family": "openai"
        },
        "stream": {
          "path": "stream",
          "model_returned": null,
          "claimed_family": null,
          "verdict": "error",
          "http_status": 503,
          "error": "HTTP 503: stream returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
          "model_requested": "gpt-4o",
          "expected_family": "openai"
        }
      }
    },
    "claude-3-5-sonnet": {
      "model_requested": "claude-3-5-sonnet",
      "expected_family": "anthropic",
      "paths": {
        "plain": {
          "path": "plain",
          "model_returned": null,
          "claimed_family": null,
          "verdict": "error",
          "http_status": 503,
          "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
          "model_requested": "claude-3-5-sonnet",
          "expected_family": "anthropic"
        },
        "stream": {
          "path": "stream",
          "model_returned": null,
          "claimed_family": null,
          "verdict": "error",
          "http_status": 503,
          "error": "HTTP 503: stream returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
          "model_requested": "claude-3-5-sonnet",
          "expected_family": "anthropic"
        }
      }
    },
    "deepseek-chat": {
      "model_requested": "deepseek-chat",
      "expected_family": "deepseek",
      "paths": {
        "plain": {
          "path": "plain",
          "model_returned": null,
          "claimed_family": null,
          "verdict": "error",
          "http_status": 503,
          "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
          "model_requested": "deepseek-chat",
          "expected_family": "deepseek"
        },
        "stream": {
          "path": "stream",
          "model_returned": null,
          "claimed_family": null,
          "verdict": "error",
          "http_status": 503,
          "error": "HTTP 503: stream returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
          "model_requested": "deepseek-chat",
          "expected_family": "deepseek"
        }
      }
    }
  },
  "errored": [
    {
      "path": "plain",
      "model_returned": null,
      "claimed_family": null,
      "verdict": "error",
      "http_status": 503,
      "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
      "model_requested": "gpt-4o",
      "expected_family": "openai"
    },
    {
      "path": "stream",
      "model_returned": null,
      "claimed_family": null,
      "verdict": "error",
      "http_status": 503,
      "error": "HTTP 503: stream returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
      "model_requested": "gpt-4o",
      "expected_family": "openai"
    },
    {
      "path": "plain",
      "model_returned": null,
      "claimed_family": null,
      "verdict": "error",
      "http_status": 503,
      "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
      "model_requested": "claude-3-5-sonnet",
      "expected_family": "anthropic"
    },
    {
      "path": "stream",
      "model_returned": null,
      "claimed_family": null,
      "verdict": "error",
      "http_status": 503,
      "error": "HTTP 503: stream returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
      "model_requested": "claude-3-5-sonnet",
      "expected_family": "anthropic"
    },
    {
      "path": "plain",
      "model_returned": null,
      "claimed_family": null,
      "verdict": "error",
      "http_status": 503,
      "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
      "model_requested": "deepseek-chat",
      "expected_family": "deepseek"
    },
    {
      "path": "stream",
      "model_returned": null,
      "claimed_family": null,
      "verdict": "error",
      "http_status": 503,
      "error": "HTTP 503: stream returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
      "model_requested": "deepseek-chat",
      "expected_family": "deepseek"
    }
  ],
  "models_checked": 0,
  "models_total": 3
}
```

</details>

### [INFO] 无法采集 tokenizer 指纹

- **编号**：`tok-000`
- **可信度**：已确认

所有模型的 tokenizer 指纹采集都失败了（请求本身没有成功返回，详见 evidence.errors）。没有 usage.prompt_tokens 就无法做指纹比对，这是本工具最可靠的检测手段。这不是「中转站没问题」，是「这一项没测成」。

<details><summary>证据</summary>

```json
{
  "errors": {
    "gpt-4o": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
    "claude-3-5-sonnet": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
    "deepseek-chat": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
  },
  "models_without_usage": [],
  "fingerprints_collected": 0
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
  "failures": {
    "gpt-4o": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
    "claude-3-5-sonnet": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
    "deepseek-chat": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
  },
  "unstable_prompts": {},
  "empty_prompts": {}
}
```

</details>

### [INFO] 未能采集到任何模型自述

- **编号**：`id-000`
- **可信度**：已确认

3 个模型的自我声明问题全部没有成功返回。这一项没有被检验，它的结论是「不知道」，不是「没问题」。

<details><summary>证据</summary>

```json
{
  "observations": {
    "gpt-4o": {
      "expected_family": "openai",
      "self_reported_families": [],
      "confirmed_families": [],
      "answers": {
        "creator": "<HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily una…>",
        "model": "<HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily una…>",
        "pivot": "<HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily una…>"
      },
      "answers_usable": 0,
      "verdict": "unchecked"
    },
    "claude-3-5-sonnet": {
      "expected_family": "anthropic",
      "self_reported_families": [],
      "confirmed_families": [],
      "answers": {
        "creator": "<HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily una…>",
        "model": "<HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily una…>",
        "pivot": "<HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily una…>"
      },
      "answers_usable": 0,
      "verdict": "unchecked"
    },
    "deepseek-chat": {
      "expected_family": "deepseek",
      "self_reported_families": [],
      "confirmed_families": [],
      "answers": {
        "creator": "<HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily una…>",
        "model": "<HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily una…>",
        "pivot": "<HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily una…>"
      },
      "answers_usable": 0,
      "verdict": "unchecked"
    }
  }
}
```

</details>

### [INFO] 标记回显检查未能完成

- **编号**：`canary-000`
- **可信度**：已确认

3 个模型都没有返回可用的响应，所以无法判断提示词是否被改写。「没查到」不等于「没问题」——这一项没有被检验。

<details><summary>证据</summary>

```json
{
  "observations": {
    "gpt-4o": {
      "error": [
        "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
        "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
      ]
    },
    "claude-3-5-sonnet": {
      "error": [
        "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
        "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
      ]
    },
    "deepseek-chat": {
      "error": [
        "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
        "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
      ]
    }
  }
}
```

</details>

### [INFO] 隐藏 reasoning 计费未能检测

- **编号**：`bill-000`
- **可信度**：已确认

3 个模型都没有返回可用的 usage，无法比较 completion_tokens 与可见内容。这一项没有被检验。

<details><summary>证据</summary>

```json
{
  "observations": {
    "gpt-4o": {
      "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
    },
    "claude-3-5-sonnet": {
      "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
    },
    "deepseek-chat": {
      "error": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
    }
  }
}
```

</details>

### [INFO] 部分参数检查未能执行

- **编号**：`params-102`
- **可信度**：已确认

有 14 项参数检查因请求失败而无法得出结论。这些参数未经过验证，不要把它们当作已通过。

<details><summary>证据</summary>

```json
{
  "errored": [
    {
      "model": "gpt-4o",
      "param": "max_tokens",
      "verdict": "error",
      "status": 503,
      "detail": "chat/completions returned 503"
    },
    {
      "model": "gpt-4o",
      "param": "stop",
      "verdict": "error",
      "status": 503,
      "detail": "chat/completions returned 503"
    },
    {
      "model": "gpt-4o",
      "param": "temperature",
      "verdict": "error",
      "status": 503,
      "detail": "chat/completions returned 503"
    },
    {
      "model": "gpt-4o",
      "param": "n",
      "verdict": "error",
      "status": 503,
      "detail": "HTTP 503"
    },
    {
      "model": "gpt-4o",
      "param": "response_format",
      "verdict": "error",
      "status": 503,
      "detail": "HTTP 503"
    },
    {
      "model": "gpt-4o",
      "param": "logprobs",
      "verdict": "error",
      "status": 503,
      "detail": "HTTP 503"
    },
    {
      "model": "gpt-4o",
      "param": "tools",
      "verdict": "error",
      "status": 503,
      "detail": "HTTP 503"
    },
    {
      "model": "claude-3-5-sonnet",
      "param": "max_tokens",
      "verdict": "error",
      "status": 503,
      "detail": "chat/completions returned 503"
    },
    {
      "model": "claude-3-5-sonnet",
      "param": "stop",
      "verdict": "error",
      "status": 503,
      "detail": "chat/completions returned 503"
    },
    {
      "model": "claude-3-5-sonnet",
      "param": "temperature",
      "verdict": "error",
      "status": 503,
      "detail": "chat/completions returned 503"
    },
    {
      "model": "claude-3-5-sonnet",
      "param": "n",
      "verdict": "error",
      "status": 503,
      "detail": "HTTP 503"
    },
    {
      "model": "claude-3-5-sonnet",
      "param": "response_format",
      "verdict": "error",
      "status": 503,
      "detail": "HTTP 503"
    },
    {
      "model": "claude-3-5-sonnet",
      "param": "logprobs",
      "verdict": "error",
      "status": 503,
      "detail": "HTTP 503"
    },
    {
      "model": "claude-3-5-sonnet",
      "param": "tools",
      "verdict": "error",
      "status": 503,
      "detail": "HTTP 503"
    }
  ]
}
```

</details>

### [INFO] 流式比对未能完成

- **编号**：`stream-000`
- **可信度**：已确认

3 个模型的流式/完整比对都没有成功返回，因此无法判断两条路径是否一致。这一项没有被检验。

<details><summary>证据</summary>

```json
{
  "observations": {
    "gpt-4o": {
      "error": "HTTP 503: stream returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
    },
    "claude-3-5-sonnet": {
      "error": "HTTP 503: stream returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
    },
    "deepseek-chat": {
      "error": "HTTP 503: stream returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}"
    }
  }
}
```

</details>

### [INFO] 长输入是否被截断未能检验

- **编号**：`ctx-000`
- **可信度**：已确认

本次没有拿到任何可判读的长输入结果（请求失败、回答被输出上限截断，或两个标记都没取回），因此**这不代表输入是完整的**，只代表这一项没有被检验。

> **建议**：先用 --probes reliability 确认中转站能不能正常服务，再重跑 --probes context；若回答被输出上限截断，请调大 --timeout 或稍后重试。

<details><summary>证据</summary>

```json
{
  "sizes_requested": [
    2000,
    8000,
    32000
  ],
  "matrix": {
    "gpt-4o": {
      "2000": {
        "depth_tokens": 2000,
        "verdict": "error",
        "status": 503,
        "detail": "HTTP 503: chat/completions returned 503 — {\"error\": {\"message\": \"upstream temporarily unavailable\", \"code\": \"upstream_error\"}}",
        "size_rejection": false
      }
    }
  },
  "verified_depth_tokens": {}
}
```

</details>

## 已核实正常

- `bill-clean` 面板可达，但未暴露上游成本

## 原始数据

完整原始返回见同目录 JSON 报告，可自行复核任何结论。
