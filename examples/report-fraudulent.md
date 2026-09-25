# relaycheck 审计报告

| 项目 | 值 |
| --- | --- |
| 目标 | `http://127.0.0.1:8200` |
| 时间 | 2026-09-26 00:31:49 +0800 |
| 耗时 | 0.5s / 150 次请求 |
| 工具版本 | 0.1.0 |
| 探针 | reliability, billing-panel, echo, tokenizer, twins, identity, canary, billing-hidden-reasoning, params, stream, context |
| 可用模型数 | 4 |
| 受测模型 | `gpt-4o`, `gemini-1.5-pro`, `claude-3-5-sonnet`, `deepseek-chat` |

## 结论

**检测到可直接定性的掉包证据**

| 严重程度 | 数量 |
| --- | --- |
| CRITICAL | 2 |
| HIGH | 4 |
| MEDIUM | 6 |
| LOW | 0 |
| INFO | 3 |
| CLEAN | 0 |

## 发现

### [CRITICAL] 疑似同一后端：claude-3-5-sonnet ≈ deepseek-chat

- **编号**：`twins-100`
- **可信度**：已确认

两个模型的 tokenizer 指纹完全相同，且在全部开放性提示上输出逐字节一致，但它们被作为不同厂商的模型出售。这是掉包的直接证据。（比对 4 个提示，其中每个提示对两个模型各采样 2 次，全部逐字一致）

> **建议**：要求中转站出示两个模型各自的上游调用凭据与账单；若为同一后端，即为以次充好，可依据宣传不实要求退赔。

<details><summary>证据</summary>

```json
{
  "models": [
    "claude-3-5-sonnet",
    "deepseek-chat"
  ],
  "claimed_families": [
    "anthropic",
    "deepseek"
  ],
  "tokenizer_identical": true,
  "identical_prompts": 4,
  "compared_prompts": 4,
  "repeats_per_prompt": 2,
  "sample": {
    "prompt": "city",
    "output": "[wide] Answer for digest 2215: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the result is 121.0; PORT=22 ssh, PORT=53 domain, PORT=80 http, PORT=443 https, PORT=3306 mysql."
  }
}
```

</details>

### [CRITICAL] 疑似同一后端：gemini-1.5-pro ≈ gpt-4o

- **编号**：`twins-100`
- **可信度**：已确认

两个模型的 tokenizer 指纹完全相同，且在全部开放性提示上输出逐字节一致，但它们被作为不同厂商的模型出售。这是掉包的直接证据。（比对 4 个提示，其中每个提示对两个模型各采样 2 次，全部逐字一致）

> **建议**：要求中转站出示两个模型各自的上游调用凭据与账单；若为同一后端，即为以次充好，可依据宣传不实要求退赔。

<details><summary>证据</summary>

```json
{
  "models": [
    "gemini-1.5-pro",
    "gpt-4o"
  ],
  "claimed_families": [
    "google",
    "openai"
  ],
  "tokenizer_identical": true,
  "identical_prompts": 4,
  "compared_prompts": 4,
  "repeats_per_prompt": 2,
  "sample": {
    "prompt": "city",
    "output": "[narrow] Answer for digest 2215: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the result is 121.0; PORT=22 ssh, PORT=53 domain, PORT=80 http, PORT=443 https, PORT=3306 mysql."
  }
}
```

</details>

### [HIGH] 注入的标记未被回显——提示词可能被改写或走了缓存

- **编号**：`canary-100`
- **可信度**：很可能

有 3/3 个模型在 2 次尝试中一次都没有复述注入的唯一标记（每次用的都是新标记）。一个正常转发请求的模型，在被要求复述时会照做。复述失败说明：或者你的提示词被中间层改写了，或者返回的是一个与本次请求无关的预置/缓存答案。

> **建议**：要求中转站提供其请求转发日志，证明 prompt 原样送达到上游。

<details><summary>证据</summary>

```json
{
  "failed_models": [
    {
      "model": "gpt-4o",
      "attempts": [
        {
          "canary": "RC-3PQ3AECYCN62",
          "echoed": false,
          "response": "[narrow] Answer for digest 3332: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the result is 121.0; PORT=22 ssh, PORT=53 domain",
          "prompt_tokens": 28
        },
        {
          "canary": "RC-CCEYH3HN84JJ",
          "echoed": false,
          "response": "[narrow] Answer for digest 3898: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the result is 121.0; PORT=22 ssh, PORT=53 domain",
          "prompt_tokens": 28
        }
      ],
      "echoed_any": false
    },
    {
      "model": "gemini-1.5-pro",
      "attempts": [
        {
          "canary": "RC-NTR46F25XR29",
          "echoed": false,
          "response": "[narrow] Answer for digest 3141: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the result is 121.0; PORT=22 ssh, PORT=53 domain",
          "prompt_tokens": 28
        },
        {
          "canary": "RC-MR2U23DZ5WLZ",
          "echoed": false,
          "response": "[narrow] Answer for digest 5234: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the result is 121.0; PORT=22 ssh, PORT=53 domain",
          "prompt_tokens": 28
        }
      ],
      "echoed_any": false
    },
    {
      "model": "claude-3-5-sonnet",
      "attempts": [
        {
          "canary": "RC-Q8UA8JG634J4",
          "echoed": false,
          "response": "[wide] Answer for digest 2242: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the result is 121.0; PORT=22 ssh, PORT=53 domain, ",
          "prompt_tokens": 51
        },
        {
          "canary": "RC-6QYRVMK5S4NH",
          "echoed": false,
          "response": "[wide] Answer for digest 5658: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the result is 121.0; PORT=22 ssh, PORT=53 domain, ",
          "prompt_tokens": 51
        }
      ],
      "echoed_any": false
    }
  ],
  "attempts_per_model": 2
}
```

</details>

### [HIGH] 疑似把隐藏的思维链按输出价格计费

- **编号**：`bill-100`
- **可信度**：很可能

共 4 个模型在极短回答下仍产生了远超可见内容的 completion_tokens，且响应中既没有 reasoning_content 字段，也没有上报 reasoning_tokens。这意味着思维链被隐藏了、但照常收你输出费。最严重的是 claude-3-5-sonnet：可见 246 字符，却计费 350 output tokens。

> **建议**：要求中转站出示上游 usage 原始返回；若思维链确实被隐藏，应按隐藏部分的价格（通常更低）或不计费处理。

<details><summary>证据</summary>

```json
{
  "suspicious_models": [
    {
      "model": "gpt-4o",
      "visible_chars": 248,
      "visible_text": "[narrow] Answer for digest 6141: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaychec",
      "completion_tokens": 268,
      "reasoning_tokens_reported": null,
      "visible_reasoning_field": false,
      "estimated_visible_tokens": 69.4,
      "billed_vs_estimated_ratio": 3.86,
      "billed_tokens_per_visible_char": 1.08,
      "verdict": "hidden-reasoning-suspected"
    },
    {
      "model": "gemini-1.5-pro",
      "visible_chars": 248,
      "visible_text": "[narrow] Answer for digest 6141: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaychec",
      "completion_tokens": 268,
      "reasoning_tokens_reported": null,
      "visible_reasoning_field": false,
      "estimated_visible_tokens": 69.4,
      "billed_vs_estimated_ratio": 3.86,
      "billed_tokens_per_visible_char": 1.08,
      "verdict": "hidden-reasoning-suspected"
    },
    {
      "model": "claude-3-5-sonnet",
      "visible_chars": 246,
      "visible_text": "[wide] Answer for digest 6141: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\"",
      "completion_tokens": 350,
      "reasoning_tokens_reported": null,
      "visible_reasoning_field": false,
      "estimated_visible_tokens": 68.9,
      "billed_vs_estimated_ratio": 5.08,
      "billed_tokens_per_visible_char": 1.42,
      "verdict": "hidden-reasoning-suspected"
    },
    {
      "model": "deepseek-chat",
      "visible_chars": 246,
      "visible_text": "[wide] Answer for digest 6141: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\"",
      "completion_tokens": 350,
      "reasoning_tokens_reported": null,
      "visible_reasoning_field": false,
      "estimated_visible_tokens": 68.9,
      "billed_vs_estimated_ratio": 5.08,
      "billed_tokens_per_visible_char": 1.42,
      "verdict": "hidden-reasoning-suspected"
    }
  ]
}
```

</details>

### [HIGH] 流式返回与完整返回的内容不一致

- **编号**：`stream-100`
- **可信度**：很可能

另有 3 个模型在同一开放式提示词下，流式与完整模式的回答不同（例如 gpt-4o：流式 3 字符 vs 完整 248 字符）；该站在 temperature=0 下可复现，因此这不是采样噪声。这意味着其中一条路径返回的不是同一个模型的输出，或者两条路径的计费口径不同。

> **建议**：要求中转站说明两条路径的上游，并出示两者的原始 usage。

<details><summary>证据</summary>

```json
{
  "deterministic_prompt_mismatches": [],
  "open_ended_mismatches": [
    {
      "model": "gpt-4o",
      "streamed_sample": "OK.",
      "plain_sample": "[narrow] Answer for digest 3595: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the result is 121.0; PORT=22 ssh, PORT=53 domain",
      "streamed_chars": 3,
      "plain_chars": 248
    },
    {
      "model": "gemini-1.5-pro",
      "streamed_sample": "OK.",
      "plain_sample": "[narrow] Answer for digest 3595: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the result is 121.0; PORT=22 ssh, PORT=53 domain",
      "streamed_chars": 3,
      "plain_chars": 248
    },
    {
      "model": "claude-3-5-sonnet",
      "streamed_sample": "OK.",
      "plain_sample": "[wide] Answer for digest 3595: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the result is 121.0; PORT=22 ssh, PORT=53 domain, ",
      "streamed_chars": 3,
      "plain_chars": 246
    }
  ],
  "observations": {
    "gpt-4o": {
      "chunks": 2,
      "ttft_s": 0.0,
      "total_s": 0.0,
      "streamed_chars": 3,
      "plain_chars": 248,
      "control_chars": 248,
      "baseline": "match",
      "usage_in_stream": {
        "prompt_tokens": null,
        "completion_tokens": null,
        "total_tokens": null,
        "cached_tokens": null,
        "cache_read_tokens": null,
        "cache_write_tokens": null,
        "reasoning_tokens": null
      },
      "model_returned": "gpt-4o",
      "finish_reason": "stop",
      "plain_finish_reason": "stop",
      "verdict": "mismatch",
      "basis": "open-ended"
    },
    "gemini-1.5-pro": {
      "chunks": 2,
      "ttft_s": 0.0,
      "total_s": 0.0,
      "streamed_chars": 3,
      "plain_chars": 248,
      "control_chars": 248,
      "baseline": "match",
      "usage_in_stream": {
        "prompt_tokens": null,
        "completion_tokens": null,
        "total_tokens": null,
        "cached_tokens": null,
        "cache_read_tokens": null,
        "cache_write_tokens": null,
        "reasoning_tokens": null
      },
      "model_returned": "gpt-4o",
      "finish_reason": "stop",
      "plain_finish_reason": "stop",
      "verdict": "mismatch",
      "basis": "open-ended"
    },
    "claude-3-5-sonnet": {
      "chunks": 2,
      "ttft_s": 0.015,
      "total_s": 0.015,
      "streamed_chars": 3,
      "plain_chars": 246,
      "control_chars": 246,
      "baseline": "match",
      "usage_in_stream": {
        "prompt_tokens": null,
        "completion_tokens": null,
        "total_tokens": null,
        "cached_tokens": null,
        "cache_read_tokens": null,
        "cache_write_tokens": null,
        "reasoning_tokens": null
      },
      "model_returned": "claude-3-5-sonnet",
      "finish_reason": "stop",
      "plain_finish_reason": "stop",
      "verdict": "mismatch",
      "basis": "open-ended"
    }
  }
}
```

</details>

### [HIGH] 长输入被静默截断：模型没有看到你发送的全部内容

- **编号**：`ctx-100`
- **可信度**：很可能

gpt-4o 在约 8000 tokens 的输入下只取回了文末的标记：文首标记 BFAXHJISCO3Z 没有出现，文末标记 L77LPQ7CXU7P 出现了；而更短的 2000 tokens 时两个标记都完整取回。两个标记都是随机生成的十二位字符串，模型不可能凭空猜出其中一个却漏掉另一个。这说明**前段输入在到达模型之前就被丢掉了**，而计费仍按你发送的完整输入计算。被截断的输入不会报错、不会警告，回答照样通顺，所以用户通常察觉不到。

> **建议**：要求商家提供该次请求的上游调用日志，与你自己发送的正文长度对账。把 prompt_tokens（服务端回报的）与你实际发送的正文规模对比：若服务端只按截断后的内容计费，至少账单是诚实的；若它按完整输入计费、而模型只看到了一部分，那就是收了钱没给货。另外可以用 --context-sizes 缩小测试范围复现，确认截断发生在哪一档之间。

<details><summary>证据</summary>

```json
{
  "cases": [
    {
      "model": "gpt-4o",
      "depth_tokens_requested": 8000,
      "actual_prompt_tokens": 6425,
      "chars_sent": 32409,
      "missing": "head",
      "truncation_direction": "头部被丢弃",
      "surviving_side": "文末",
      "head_code": "BFAXHJISCO3Z",
      "tail_code": "L77LPQ7CXU7P",
      "answer": "BETA-L77LPQ7CXU7P",
      "depths_that_passed": [
        2000
      ],
      "passed_at_tokens": 2000,
      "failed_at_tokens": 6425
    }
  ],
  "sizes_tested": [
    2000,
    8000,
    32000
  ],
  "note": "标记是每次请求随机生成的十二位字符串，写在正文的最前面和最后面。取回哪一个，就证明哪一段输入真的到达了模型。"
}
```

</details>

### [MEDIUM] 面板自报的上游成本暴露了各模型加价倍率

- **编号**：`bill-201`
- **可信度**：已确认

该面板在用量接口里同时返回了『向你收取的金额』(cost) 和『它自己记的上游成本』(account_cost)。两者相除即为真实加价倍率。其中 deepseek-chat 加价 20.75x。这是平台自己的账，无法用话术否认。

> **建议**：用于对账与议价；要求商家解释加价倍率与其宣传是否一致。

<details><summary>证据</summary>

```json
{
  "per_model_margin": [
    {
      "model": "deepseek-chat",
      "requests": 107,
      "charged": 0.98449,
      "upstream_cost": 0.047444,
      "markup_x": 20.75
    },
    {
      "model": "gpt-4o",
      "requests": 73,
      "charged": 0.454471,
      "upstream_cost": 0.454471,
      "markup_x": 1.0
    }
  ]
}
```

</details>

### [MEDIUM] 模型共享同一 tokenizer：claude-3-5-sonnet, deepseek-chat

- **编号**：`tok-100`
- **可信度**：已确认

这些模型在全部 8 个固定测试串上返回了完全一致的 prompt_tokens，说明它们使用同一个 tokenizer（同一模型家族）。而它们自称的厂商并不相同（['anthropic', 'deepseek']），所以其中至少有一个名不副实。注意：相同 tokenizer 只证明『同家族』，不等同于『同权重』——请结合 twins 探针的行为比对一起判断。

> **建议**：要求中转站解释这些模型为何共享 tokenizer；若宣称来自不同厂商，即为虚假宣传。

<details><summary>证据</summary>

```json
{
  "group": [
    "claude-3-5-sonnet",
    "deepseek-chat"
  ],
  "claimed_families": [
    "anthropic",
    "deepseek"
  ],
  "shared_tokenizer_is_expected": false,
  "vector": {
    "en": 85,
    "cjk": 233,
    "code": 194,
    "emoji": 109,
    "mixed": 28,
    "ws": 15,
    "digits": 22,
    "json": 179
  },
  "string_lengths": {
    "en": 180,
    "cjk": 136,
    "code": 236,
    "emoji": 64,
    "mixed": 39,
    "ws": 36,
    "digits": 88,
    "json": 177
  }
}
```

</details>

### [MEDIUM] 模型共享同一 tokenizer：gemini-1.5-pro, gpt-4o

- **编号**：`tok-100`
- **可信度**：已确认

这些模型在全部 8 个固定测试串上返回了完全一致的 prompt_tokens，说明它们使用同一个 tokenizer（同一模型家族）。而它们自称的厂商并不相同（['google', 'openai']），所以其中至少有一个名不副实。注意：相同 tokenizer 只证明『同家族』，不等同于『同权重』——请结合 twins 探针的行为比对一起判断。

> **建议**：要求中转站解释这些模型为何共享 tokenizer；若宣称来自不同厂商，即为虚假宣传。

<details><summary>证据</summary>

```json
{
  "group": [
    "gemini-1.5-pro",
    "gpt-4o"
  ],
  "claimed_families": [
    "google",
    "openai"
  ],
  "shared_tokenizer_is_expected": false,
  "vector": {
    "en": 39,
    "cjk": 135,
    "code": 103,
    "emoji": 63,
    "mixed": 15,
    "ws": 7,
    "digits": 7,
    "json": 98
  },
  "string_lengths": {
    "en": 180,
    "cjk": 136,
    "code": 236,
    "emoji": 64,
    "mixed": 39,
    "ws": 36,
    "digits": 88,
    "json": 177
  }
}
```

</details>

### [MEDIUM] 中转站接受但忽略部分参数：logprobs(2)、max_tokens(2)、n(2)、response_format(2)、stop(2)、temperature(2)、tools(2)

- **编号**：`params-100`
- **可信度**：已确认

以下参数被接口正常接收（返回 200），但行为上没有任何效果：logprobs(2)、max_tokens(2)、n(2)、response_format(2)、stop(2)、temperature(2)、tools(2)。这意味着你传入的控制参数被静默丢弃。对 max_tokens 而言，这直接关系到你的账单。

> **建议**：要求中转站修复参数透传；在修复前，不要把依赖这些参数的自动化流程接到该站。

<details><summary>证据</summary>

```json
{
  "ignored": [
    {
      "model": "gpt-4o",
      "param": "max_tokens",
      "verdict": "ignored",
      "requested_max_tokens": 16,
      "billed_completion_tokens": 268,
      "finish_reason": "stop",
      "visible_chars": 248
    },
    {
      "model": "gpt-4o",
      "param": "stop",
      "verdict": "ignored",
      "stop": [
        "5"
      ],
      "baseline_chars": 248,
      "stopped_chars": 248,
      "stopped_response": "[narrow] Answer for digest 6068: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the resu",
      "note": "带 stop 的返回与不带 stop 完全一致，说明 stop 被静默丢弃"
    },
    {
      "model": "gpt-4o",
      "param": "temperature",
      "verdict": "ignored",
      "temp0_runs_identical": true,
      "temp1_5_runs_differ": false,
      "samples": {
        "temp0_a": "[narrow] Answer for digest 8888: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaychec",
        "temp0_b": "[narrow] Answer for digest 8888: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaychec",
        "temp1_5_a": "[narrow] Answer for digest 8888: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaychec",
        "temp1_5_b": "[narrow] Answer for digest 8888: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaychec"
      },
      "note": "temperature=1.5 的两次调用结果逐字相同：temperature 被静默丢弃，每个请求都等价于同一次固定采样。"
    },
    {
      "model": "gpt-4o",
      "param": "n",
      "verdict": "ignored",
      "requested_n": 2,
      "returned_choices": 1
    },
    {
      "model": "gpt-4o",
      "param": "response_format",
      "verdict": "ignored",
      "parsed_as_json": false,
      "response": "[narrow] Answer for digest 4106: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the resu"
    },
    {
      "model": "gpt-4o",
      "param": "logprobs",
      "verdict": "ignored",
      "logprobs_present": false,
      "note": "缺失 logprobs 会让依赖置信度的下游工具出错"
    },
    {
      "model": "gpt-4o",
      "param": "tools",
      "verdict": "ignored",
      "tool_calls_present": false,
      "response": "[narrow] Answer for digest 1337: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the resu"
    },
    {
      "model": "gemini-1.5-pro",
      "param": "max_tokens",
      "verdict": "ignored",
      "requested_max_tokens": 16,
      "billed_completion_tokens": 268,
      "finish_reason": "stop",
      "visible_chars": 248
    },
    {
      "model": "gemini-1.5-pro",
      "param": "stop",
      "verdict": "ignored",
      "stop": [
        "5"
      ],
      "baseline_chars": 248,
      "stopped_chars": 248,
      "stopped_response": "[narrow] Answer for digest 6068: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the resu",
      "note": "带 stop 的返回与不带 stop 完全一致，说明 stop 被静默丢弃"
    },
    {
      "model": "gemini-1.5-pro",
      "param": "temperature",
      "verdict": "ignored",
      "temp0_runs_identical": true,
      "temp1_5_runs_differ": false,
      "samples": {
        "temp0_a": "[narrow] Answer for digest 8888: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaychec",
        "temp0_b": "[narrow] Answer for digest 8888: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaychec",
        "temp1_5_a": "[narrow] Answer for digest 8888: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaychec",
        "temp1_5_b": "[narrow] Answer for digest 8888: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaychec"
      },
      "note": "temperature=1.5 的两次调用结果逐字相同：temperature 被静默丢弃，每个请求都等价于同一次固定采样。"
    },
    {
      "model": "gemini-1.5-pro",
      "param": "n",
      "verdict": "ignored",
      "requested_n": 2,
      "returned_choices": 1
    },
    {
      "model": "gemini-1.5-pro",
      "param": "response_format",
      "verdict": "ignored",
      "parsed_as_json": false,
      "response": "[narrow] Answer for digest 4106: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the resu"
    },
    {
      "model": "gemini-1.5-pro",
      "param": "logprobs",
      "verdict": "ignored",
      "logprobs_present": false,
      "note": "缺失 logprobs 会让依赖置信度的下游工具出错"
    },
    {
      "model": "gemini-1.5-pro",
      "param": "tools",
      "verdict": "ignored",
      "tool_calls_present": false,
      "response": "[narrow] Answer for digest 1337: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the resu"
    }
  ],
  "counts": {
    "max_tokens": 2,
    "stop": 2,
    "temperature": 2,
    "n": 2,
    "response_format": 2,
    "logprobs": 2,
    "tools": 2
  }
}
```

</details>

### [MEDIUM] 流式响应不返回 usage，导致无法对账

- **编号**：`stream-101`
- **可信度**：已确认

有 3 个模型在请求 include_usage 后仍未在流中返回 usage。用户因此无法核对流式请求被计费了多少 token，这为虚报用量提供了空间。

> **建议**：标准做法是在最后一个 chunk 返回 usage；缺失属于兼容性缺陷。

<details><summary>证据</summary>

```json
{
  "models_without_stream_usage": [
    {
      "model": "gpt-4o",
      "chunks": 2
    },
    {
      "model": "gemini-1.5-pro",
      "chunks": 2
    },
    {
      "model": "claude-3-5-sonnet",
      "chunks": 2
    }
  ]
}
```

</details>

### [MEDIUM] 响应体自称的模型属于另一个厂商

- **编号**：`echo-100`
- **可信度**：很可能

共 4 处响应在 ``model`` 字段里自报的名字，与请求的模型名不属于同一个厂商。例如以「gemini-1.5-pro」发出的请求（厂商 google），响应体自称是 gpt-4o（厂商 openai）。全部陌生的自报名：claude-3-5-sonnet, gpt-4o，涉及厂商：anthropic, openai。这个字段由上游填写，站方不改写它，通常意味着请求真的落在了另一个后端上。**但它不是铁证**：站方也可能套了一层自己的第三方上游命名，或只是把响应原样透传而路由与计费都没有问题。请把这一条与 twins、tokenizer 两条放在一起看，以那两条的结论为准。

> **建议**：核对下单的模型与实际收到的模型是否同一个；若站方坚持是同一个，要求它给出这次请求的上游调用记录。同时运行 --probes tokenizer,twins 做行为层面的交叉验证。

<details><summary>证据</summary>

```json
{
  "cross_vendor": [
    {
      "path": "plain",
      "model_returned": "gpt-4o",
      "claimed_family": "openai",
      "verdict": "cross_vendor",
      "http_status": 200,
      "error": null,
      "model_requested": "gemini-1.5-pro",
      "expected_family": "google"
    },
    {
      "path": "stream",
      "model_returned": "gpt-4o",
      "claimed_family": "openai",
      "verdict": "cross_vendor",
      "http_status": 200,
      "error": null,
      "model_requested": "gemini-1.5-pro",
      "expected_family": "google"
    },
    {
      "path": "plain",
      "model_returned": "claude-3-5-sonnet",
      "claimed_family": "anthropic",
      "verdict": "cross_vendor",
      "http_status": 200,
      "error": null,
      "model_requested": "deepseek-chat",
      "expected_family": "deepseek"
    },
    {
      "path": "stream",
      "model_returned": "claude-3-5-sonnet",
      "claimed_family": "anthropic",
      "verdict": "cross_vendor",
      "http_status": 200,
      "error": null,
      "model_requested": "deepseek-chat",
      "expected_family": "deepseek"
    }
  ],
  "self_reported_backends": [
    "claude-3-5-sonnet",
    "gpt-4o"
  ],
  "models_checked": 4,
  "models_total": 4
}
```

</details>

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

### [INFO] 模型自称的厂商与售卖名称不一致（线索，非结论）

- **编号**：`id-100`
- **可信度**：疑似

共 2 个模型在被问及自身来历时，说出了与售卖名称不同的厂商/模型家族。例如以「gemini-1.5-pro」出售的模型自称属于 openai。**这是一条线索，不是结论**：模型自述本身极不可靠，非 OpenAI 的模型自称 OpenAI 是常见现象（训练语料所致），单凭自述不足以判定掉包。要成立，必须由本报告中的 tokenizer 指纹或行为比对（twins）独立指向同一结论——请以那两条的结论为准。

> **建议**：用 tokenizer 指纹和确定性输出比对交叉验证；若两者同时指向另一个家族，则可确认为掉包。

<details><summary>证据</summary>

```json
{
  "contradictions": [
    {
      "model": "gemini-1.5-pro",
      "expected_family": "google",
      "reported_family": [
        "openai"
      ],
      "quote": "I was created by OpenAI.",
      "answers": {
        "creator": "I was created by OpenAI.",
        "model": "[narrow] Answer for digest 8051: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the result is 121.0; PORT=22 ssh, PORT=53 domain, PORT=80 http, PORT=443 https, PORT=3306 mysql.",
        "pivot": "I was created by OpenAI.",
        "creator#2": "I was created by OpenAI.",
        "pivot#2": "I was created by OpenAI."
      }
    },
    {
      "model": "deepseek-chat",
      "expected_family": "deepseek",
      "reported_family": [
        "minimax"
      ],
      "quote": "I was developed by MiniMax.",
      "answers": {
        "creator": "I was developed by MiniMax.",
        "model": "[wide] Answer for digest 8051: the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; {\"tool\":\"relaycheck\",\"value\":\"relaycheck\",\"n\":7}; the result is 121.0; PORT=22 ssh, PORT=53 domain, PORT=80 http, PORT=443 https, PORT=3306 mysql.",
        "pivot": "I was developed by MiniMax.",
        "creator#2": "I was developed by MiniMax.",
        "pivot#2": "I was developed by MiniMax."
      }
    }
  ]
}
```

</details>

## 原始数据

完整原始返回见同目录 JSON 报告，可自行复核任何结论。
