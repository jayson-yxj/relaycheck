# relaycheck

[![CI](https://github.com/jayson-yxj/relaycheck/actions/workflows/ci.yml/badge.svg)](https://github.com/jayson-yxj/relaycheck/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Detect whether an LLM API **relay** (proxy / reseller / aggregator) is **swapping models**
or **inflating your bill**.

You paid for `claude-3-5-sonnet` — did you actually get `deepseek-chat`? You asked for
`max_tokens=16` — why does the invoice say 268 output tokens? This tool answers both
questions with **reproducible evidence**.

> relaycheck is **read-only**. It only sends chat completions and only reads endpoints the
> panel itself exposes publicly or in relation to your own account. It does not register
> accounts, redeem anything, or modify remote state.

*[中文说明见 `README.md`](README.md)*

---

## Why it exists

A relay can do all of this server-side, and you will not see it in the response:

| What it does | What you see |
|---|---|
| Rewrites your requested `model` field to a cheaper model | The `model` field in the response is rewritten back to the name you asked for |
| Rewrites your prompt and forwards it to a generic backend | A well-formed, fluent answer |
| Hides the reasoning chain but bills it at output price | You asked for one word and got billed 350 tokens |
| Accepts `max_tokens` / `stop` / `tools` and ignores them | HTTP 200, parameters silently vanish |
| Returns a short answer when streaming, a different one when not | Both paths look fine on their own |
| Forwards only the last few thousand characters of a long input, bills the whole thing | The answer is still fluent; the model just seems oddly forgetful |

**None of these leave a trace you can read directly.** But they do leave **behavioural
fingerprints** — when one backend pretends to be two models, its tokenizer and its output
give it away.

---

## One hard rule: `CLEAN` must mean *verified*

This is the single most important invariant in the tool, more important than any individual probe.

`CLEAN` in the report does not mean "nothing was found". It means "**this check ran and the
result was good**". The difference is fatal in the real world:

- the relay is flaky and all 8 requests time out;
- if "we got no answer" were treated as "the answer looked fine", the report would come back **all green**;
- and an all-green report is worse than no report at all: it launders a real model swap into a clean bill of health.

So every probe must distinguish six outcomes, and only the first may write `CLEAN`:

| Outcome | What it looks like in the report |
|---|---|
| The check ran, result normal | `CLEAN` (e.g. `canary-clean` / `params-clean`) |
| The check never ran | `INFO` (`budget-000`, plus `tok-000` / `echo-000` / `twins-000` / `id-000` / `canary-000` / `bill-000` / `bill-202` / `params-102` / `stream-000` / `ctx-000` / `ctx-102`) |
| The check ran, but had **nothing judgeable to work on** | `INFO` (`params-103`: samples empty or too short; `stream-103`: both paths returned nothing) |
| The check ran, but **the station cannot reproduce itself, so the comparison is void** | `INFO` (`params-104`: two `temperature=0` calls differed; `stream-104`: two identical requests differed, so a stream/plain diff means nothing) |
| The check ran and **saw something worth recording that is not an accusation** | `INFO` (`id-100`: the model's self-report names a different vendor than the sale name; `echo-101`: the response names a different model of the same vendor; `id-101`: the model's self-report is unstable; `id-102`: a self-report could not be rechecked; `bill-201`: the panel publishes upstream cost with no markup) |
| The check ran, anomaly found | `LOW` / `MEDIUM` / `HIGH` / `CRITICAL` |

> `*-000` ids are **reserved** to mean "this check did not run"; they are never an accusation.
> `budget-000` means the probe was cut off by the wall-clock budget, `bill-202` means no panel
> endpoint was reachable. Treat them as **blank**, not as **pass**.
>
> That third row is the one that used to be missing. The first two both say
> "I could not measure"; the third says "**I measured, and the empty string in my hand proves
> nothing**". Reading an empty string as "the two paths disagree" or "the parameter was ignored"
> means charging someone else's relay for your own budgeting mistake. It was added after being
> burned, and the `reasoning` scenario plus its regression test now hold that line.
>
> The fourth row is the same mistake in a different disguise. This time the hand does not hold an
> empty string but **two genuinely different texts** — and yet the station cannot reproduce *itself*
> at `temperature=0` (mixture-of-experts routing and batching both look like this), so "the streamed
> answer differs from the plain answer" and "two identical calls differed" are both **what noise
> looks like**, not evidence. Hence the rule: **measure the noise floor before drawing a conclusion**.
> When the floor cannot be measured, say "cannot judge" — do not turn noise into a HIGH.
> The `noisy` scenario and its regression test hold that line.
>
> The fifth row guards the opposite mistake: **something really was observed, and it still cannot
> carry an accusation.** A model claiming to be from another vendor (`id-100`) sounds like a catch,
> but a self-report is just what the training data was full of — a non-OpenAI model answering
> "OpenAI" is routine — so it is a note, not an accusation, and it reports at `INFO`, not `LOW`.
> If the same model cannot even repeat itself across two identical questions (`id-101`), it does not
> count at all. A response naming a different model of the *same* vendor (`echo-101`), or a panel
> whose own published upstream cost shows no markup (`bill-201` demoted to INFO), belong in the same
> box: **worth writing down, not worth writing into the accusation column.** Shoving these into
> `MEDIUM` and deleting them outright are the same mistake in two disguises — the first convicts an
> honest station, the second lets a reader assume the absence of a line means the absence of a
> problem.

This rule is **guarded by tests**, not by good intentions: the `dead` scenario (panel alive,
but every completion fails) asserts that no "declared clean without checking" finding id may
appear. The `slow` scenario (correct behaviour, very slow) asserts that a truncated probe
reports itself as **incomplete** rather than passing.

> For users, in one sentence: when you see an `INFO` `*-000` / `*-102`, read it as **blank**,
> not as **pass**. What you want is "re-run during a stable period", not "looks fine".

---

## How the detection works (and why the evidence holds up)

### 1. Tokenizer fingerprint — the cheapest, hardest evidence

Send 8 frozen test strings (English / Chinese / code / emoji / mixed / whitespace / digits /
JSON) to each model with `max_tokens=1`, and read `usage.prompt_tokens`.

**If two models claiming different vendors return identical token counts on all 8 strings,
they share one tokenizer.** One tokenizer means one model family — different vendors cannot
share one.

Cost: 9 requests per model, each emitting a single token.

> Honest boundary: an identical tokenizer only proves **same family**, not **same weights**
> (`gpt-4o` and `gpt-4o-mini` share a tokenizer). So the probe first asks what vendor each
> *name* claims to belong to:
>
> - the names contradict each other (`claude-3-5-sonnet` and `deepseek-chat` sharing a
>   tokenizer) → `tok-100` **MEDIUM**;
> - the names already claim the same vendor (`gpt-4o` and `gpt-4o-mini`) → `tok-101`
>   **INFO**, recorded as a fact only, explicitly labelled "this is **not** model-swap evidence".
>
> One rule: **only contradictory names constitute evidence.** A name that cannot be resolved
> to a vendor is treated as *unknown*, never read as *conflicting*.

### 2. The model name the response reports — hard evidence, nearly free

Every OpenAI-compatible gateway **must** fill in the `model` field of a response.
A station that really forwards your request to another backend usually passes that
backend's name straight through — rewriting it is extra work, and no client checks
the field.

So "the model you paid for" and "the model the upstream says it is" can be put
into one piece of evidence for the price of **two requests per model** (one plain,
one streaming — those are two separate places where the same field has to be
filled in).

The check does not treat that field as testimony:

| Case | Verdict |
|---|---|
| identical to the request | `echo-clean` |
| different, same vendor (`gpt-4o` → `gpt-4o-2024-11-20`) | `echo-101` **INFO**, says outright "this is **not** an accusation" |
| different, different vendor (ask for `deepseek-chat`, response says `claude-3-5-sonnet`) | `echo-100` **MEDIUM / likely** |
| field empty, or a name with no recognisable vendor | `echo-102` **INFO**: cannot be compared |

> Honest boundary: the field is filled in upstream, so the station can forge it,
> rewrite it into the sales name, or leave it empty. A cross-vendor conflict
> *usually* means the request really did land on another backend, but it can also
> mean the station sits behind a third-party upstream with its own naming. So
> `echo-100` is "likely", not "confirmed", and its wording asks you to read it
> together with `twins` and `tokenizer`.
>
> The other direction matters just as much: `echo-clean` is only emitted when every
> model answered comparably on **both** paths. A failed request or an exhausted
> budget yields `echo-000` INFO — "we could not measure it" is never allowed to
> become "it looked fine".

### 3. Twin comparison — direct evidence of a swap

Send the same set of **open-ended** prompts to every model (`temperature=0`) and compare
outputs pairwise.

The critical design point: **the prompts must be open-ended.**
An early version used prompts with a unique correct answer, like "write 1 to 20" or
"compute 17×23" — that is a trap: two genuinely *different* models both emit `1 2 3 ... 20`,
so every honest relay gets convicted. Byte-identical output is only meaningful evidence when
the answer space is large enough.

So the prompts are: an invented protocol name, a haiku about packet loss, colours that do not
exist, an imaginary city.

Each prompt is run **twice** against the same model. Only prompts that agreed with themselves
byte-for-byte enter the comparison — if the backend is not reproducible, cross-model
differences prove nothing and cross-model agreement is just luck. Unstable prompts are
dropped and **reported as a limitation**, never silently turned into a false negative.

Severity ladder:

| Situation | Verdict |
|---|---|
| Same tokenizer + identical output + claims different vendors | **CRITICAL** (confirmed) |
| Claims different vendors + identical output | HIGH |
| Same tokenizer + identical output + **claims the same vendor** | LOW (`twins-101`, no conclusion drawn) |
| Identical output (vendor unknown) | MEDIUM (suspected) |

That last LOW is deliberately depressed. Byte-identical output from two names of the same
vendor may be a **legitimate alias** (one set of weights sold under two names, e.g. `gpt-4o`
and `chatgpt-4o-latest`), or it may be one backend sold to you as two tiers.
**Output comparison alone cannot tell those apart**, so it reports LOW and draws no
conclusion — LOW does not trip the default `--fail-on high`, so an honest alias pair is never
convicted.

### 4. Canary injection — did the prompt arrive intact?

Embed a unique marker `RC-XXXXXXXXXXXX` in the prompt and ask the model to repeat it. A relay
that forwards faithfully will comply. No echo means the prompt was rewritten in the middle,
or you were served a canned/cached answer.

### 5. Hidden reasoning billing

Ask for one word (small `max_tokens`) and check whether `completion_tokens` is grossly out of
proportion to the visible characters. If the response has neither a `reasoning_content` field
nor reported `reasoning_tokens`, yet the completion count is large, then the reasoning chain
was hidden **and billed at output price**.

### 6. Parameter passthrough

Every item is a **behavioural test**; HTTP 200 is not trusted:

- `max_tokens`: ask for 16, get billed > 24 → ignored
- `stop`: differential test — take a baseline first (containing the stop token), then request
  with `stop`. If honoured, output is truncated; if ignored, output is exactly the same
- `temperature`: two-stage — `temperature=0` must be reproducible, and `temperature=1.5` must
  differ between two runs (identical means sampling was dropped)
- `n=2` / `response_format` / `logprobs` / `tools`: check whether the response actually contains them

> The `temperature` check has one counter-intuitive part: **an unreproducible `temperature=0`
> is not proof that the parameter was dropped.** Mixture-of-experts routing and batched backends
> are non-deterministic by nature, and an honest relay behaves the same way. So "two calls
> differed" is reported on its own as `params-104` (INFO, whose text **explicitly does not accuse
> anyone of ignoring the parameter**), and `params-100` (MEDIUM) is reserved for the case where
> there were judgeable samples *and* those samples contradict each other. When the two cannot be
> told apart, do not accuse — that is the most basic discipline in this project.

### 7. Stream integrity

Run the same prompt once streaming and once not, **with identical sampling parameters**, and then
run the non-streaming call **a second time as a control**. Three things, judged in order:

1. If two identical non-streaming requests already differ, the station has no reproducible output
   at all and the open-ended comparison is **void** (noise is not evidence of a model swap). The
   probe then falls back to a **deterministic question** (`37 * 41` — one correct answer, which
   sampling noise cannot change). Still agreeing → `stream-104` (INFO): not verified, but said
   plainly. A real disagreement there → `stream-100` (HIGH).
2. The two paths disagree → `stream-100` (HIGH): not the same backend, or billed differently.
3. The stream returns no usage → `stream-101` (MEDIUM): you cannot reconcile your bill.

> The first version got this wrong: the streaming path sent no `temperature` while the plain path
> sent `temperature=0`, and the resulting difference between two differently-sampled calls was
> reported as a model swap at HIGH severity. Aligning the parameters was not enough, which is why
> step 1 above exists.

### 8. Panel self-reported billing basis

Read `/v1/sub2api/billing`, `/v1/usage`, `/api/v1/settings/public` and friends.
If `/v1/usage` returns both `cost` (what you were charged) and `account_cost` (what the panel
recorded as its own upstream cost), the markup is no longer a guess — it is **the platform's
own books**.

> **When a merchant claims "we bill per request"**: the authoritative value is
> `billing_scope` from the panel's `/v1/sub2api/billing`. `billing_scope=token` means
> per-token billing, and the verbal claim is false.

### 9. Reliability — first confirm the thing can work at all

This is not model-swap detection, but without it every conclusion above may be unsound.

The probe sends 8 minimal requests (`max_tokens=8`) with **retries disabled**, and reports the
raw success rate and latency percentiles. Disabling retries is deliberate: with retries on you
cannot see the true failure rate, and that is precisely what is being measured.

- Failure rate > 20% → `HIGH`. A relay that cannot serve one sentence reliably cannot do
  stable work, and it also means every other probe's conclusions may be incomplete.
- Median latency > 15s → `MEDIUM`. Usually means shared-pool queueing or throttling, not a
  direct connection to the official API.

The result is also written into the report's notes, so a reader immediately knows whether the
later "nothing found" verdicts were genuinely clean or simply never finished.

### 10. Context integrity — is a long input being quietly cut down?

This targets a behaviour that is easy to miss and expensive to pay for: a relay advertises a 128k
context, forwards only the **last few thousand characters** to the backend, drops the rest, and
bills you for the full length you sent.

It is much harder to spot than a model swap, because **nothing looks wrong**: no error, no
warning, HTTP 200, a fluent answer, and a `prompt_tokens` that looks plausible. You just conclude
the model has a bad memory.

The method is a two-marker probe:

1. Bury a marker `ALPHA-<12 random chars>` at the **start** of a long input and
   `BETA-<12 random chars>` at the **end**, fill the middle with meaningless text, and ask the
   model to echo both markers in order.
2. Grow the input in steps (default 2000 → 8000 → 32000 tokens), and stop the moment truncation
   appears rather than paying to go deeper.

The verdict is differential and does not depend on the model's memory:

| What you observe | Conclusion |
|---|---|
| both markers returned | nothing was truncated up to that depth |
| only the tail marker | **the head was dropped** (the usual way) |
| only the head marker | the tail was dropped |
| neither marker | treated as **unreadable**, not as evidence |

Both markers are random twelve-character strings, so a model cannot plausibly invent one and miss
the other. "Only the tail came back" therefore has exactly one explanation.

- An easier depth passed intact as a control → `HIGH` / likely
- Markers already lost at the shallowest depth, no control → `MEDIUM` / suspected
- Explicitly rejected (413 / 414 / 422, or a 400 that mentions the token limit) → `INFO`.
  That is **honest disclosure, not fraud** — plenty of services work this way.

> An honest relay also starts dropping content past some depth — that is its own backend's limit.
> So `ctx-clean` says "arrived intact **within the tested range**" and spells out that this is only
> a lower bound, not proof that the advertised context length holds. The probe never reads it as
> "supports 128k".

Each rung sends a request of thousands to tens of thousands of tokens, so this probe is
**expensive**. It is therefore off by default (opt in with `--probes all`) and tests one model by
default.

---

## Install

```bash
# Straight from GitHub, no clone required
pip install "git+https://github.com/jayson-yxj/relaycheck.git"

# From a source checkout; installs the `relaycheck` command
pip install .

# Development mode (pulls in pytest)
pip install -e ".[dev]"

# Or skip installing entirely
python -m relaycheck.cli --help
```

The only dependency is `requests`. Python ≥ 3.9.

On versions: the syntax was checked file by file with `ast.parse(..., feature_version=(3, 9))`
(20 files, 0 incompatible), and the end-to-end run was exercised on 3.11. CI declares
3.9 / 3.11 / 3.13 on Linux plus one Windows and one macOS leg — but **that workflow has
never actually run**, so whether it is green is unknown until the first push.

---

## Usage

```bash
# Minimal: auto-discover models, run the default probes
relaycheck --base-url https://api.example.com --api-key sk-xxxx

# Name the models to compare (twin detection is most valuable across claimed vendors)
relaycheck -u https://api.example.com -k sk-xxxx \
    --models "gpt-4o,claude-3-5-sonnet,deepseek-chat,gemini-1.5-pro"

# All probes (including parameter passthrough, stream integrity and context truncation)
relaycheck -u https://api.example.com -k sk-xxxx --probes all

# Only the three cheapest, hardest probes
relaycheck -u https://api.example.com -k sk-xxxx --probes echo,tokenizer,twins

# Only check whether long inputs are being quietly cut down (every request is expensive)
relaycheck -u https://api.example.com -k sk-xxxx --probes context \
    --context-sizes 8000,32000,128000

# Target very slow? Test reliability alone first to see whether it can serve at all
relaycheck -u https://api.example.com -k sk-xxxx --probes reliability
```

The key can also come from the environment: `RELAYCHECK_API_KEY` / `OPENAI_API_KEY`.

### Common options

| Option | Meaning |
|---|---|
| `-m, --models` | Models under test, comma-separated. When omitted, chosen from `/v1/models` for **vendor diversity** |
| `--max-models` | How many at most (default 6, controls request count and spend) |
| `--probes` | A subset of probes, or `all` |
| `--delay` | Seconds between requests (default 0.4, to avoid rate limits) |
| `--timeout` | Per-request timeout in seconds (default 60) |
| `--budget` | Wall-clock budget **per probe** in seconds (default 240). See below |
| `--reliability-samples` | Reliability probe sample count (default 8) |
| `--context-sizes` | Context probe depths in tokens (default `2000,8000,32000`); deepens step by step and stops at the first truncation |
| `--context-max-models` | How many models the context probe tests (default 1; it is expensive) |
| `--fail-on` | Exit 1 when this severity is reached (default high) |
| `--list-probes` / `--list-models` | Print the list and stop |

### About `--budget`: slow relays cannot hang the tool

A great many real-world relays are **slow and unstable**. The author measured one: 9 minimal
requests (one word, output capped at 8 tokens), 5 of which did not return within 45 seconds;
the same model answered in 1.9s once and timed out at 45s the next time. Without a budget, the
`tokenizer` probe's 27 requests take tens of minutes and look exactly like a hang.

So every probe has its own wall-clock budget (default 240s). When the budget runs out:

- the probe **stops immediately**; the client is bound to the same deadline before each request
  and on the request timeout itself;
- the report **states plainly** that "this probe ended early on its timeout budget and the
  unfinished part has no conclusion", and lists how much completed;

**Important**: a truncated probe is never shown as "passed". An incomplete report is better
than a false clean bill. Raise `--budget` to run more completely (`--budget 0` means unlimited),
but first decide whether the target is worth waiting for.

On a slow relay you will see heartbeat lines, which distinguish "slow" from "hung":

```
  → reliability …
      · DeepSeek v4 Flash 第 1/8 次探测中…
      · DeepSeek v4 Flash 第 2/8 次探测中…
    ✓ reliability: high (8 请求 / 241.3s) [预算用尽，未跑完]
      成功率 3/8（失败率 62%），中位延迟 2.9s
```

### Output

- `report.md` — a readable report, **usable directly as a complaint attachment**
- `report.json` — all raw data, so any conclusion can be independently re-checked

Exit codes: `0` clean / `1` reached `--fail-on` / `2` run failed.

Those three must never overlap, which is a hard rule in the code: **no exception may ever exit
with status 1.** If a crash and a real accusation both return 1, a shell cannot tell them apart —
the script reads the crash as "it found something". By the same rule, an unprintable character
must not be able to abort an audit: a Windows console defaults to a legacy code page, `✓` is not
in cp936, and the old code raised `UnicodeEncodeError` in the middle of the probe loop — the audit
died, no report was written, and the exit status was still 1. Now only `errors` is relaxed (never
`encoding`), so an unrepresentable glyph degrades to `?` while Chinese still renders correctly in
`cmd.exe`.

`report.md` / `report.json` are always UTF-8, independent of the console code page. To make stdout
UTF-8 as well (e.g. redirecting into a log you later read with UTF-8 tools), set `PYTHONUTF8=1`.

Line endings are always `\n`, on Windows too. Otherwise the same audit on two machines produces a
whole-file diff (every line "changed") and the one field that actually changed is invisible.

---

## How to use the report

Every finding carries two independent fields: **severity** and **confidence**. These must stay
separate: "this is bad" and "we can be sure this is bad" are different claims.

- `CRITICAL` + *confirmed* = evidence you can take straight to a confrontation
- `HIGH` + *likely* = a strong inference; only the merchant's logs can settle it
- `MEDIUM` + *suspected* = a signal worth a human look, **do not** accuse on it alone

How to phrase it (using `twins-100` as the example):

> Your `claude-3-5-sonnet` and `deepseek-chat` returned exactly identical `prompt_tokens` on
> 8 frozen test strings, and byte-identical output on 4 open-ended prompts, each sampled twice
> per prompt. Please produce the upstream call credentials and invoices for each of these two
> models.

---

## Honest limitations

This tool's conclusions have hard boundaries. **Read this section before use**, or you will
make accusations you cannot support.

1. **Same tokenizer ≠ same weights.** It only proves same family. Behavioural comparison is required.
2. **Identical behaviour may be caching, not a swap.** The canary probe exists to separate the two.
3. **Model self-reports are unreliable.** Models hallucinate their own identity, and a non-OpenAI model answering "OpenAI" is extremely common (it is what the training data is full of) — and it happens **on honest stations too**: in a live run, an ordinary relay reselling `deepseek-v4-flash` had that model claim OpenAI on both rounds. So `id-100` is an `INFO` *suspected* finding whose title literally says "a lead, not a conclusion": it only records *what the model says it is*, and `tokenizer` / `twins` produce the actual verdict. It has gone MEDIUM → LOW → INFO for one and the same reason: **something that cannot carry an accusation does not belong in the accusation column**, or every honest station reselling DeepSeek collects a LOW. There is also a second gate: **a self-report counts only if the model says it twice.** When a model names a foreign vendor, the probe asks the same question again; only a second answer naming the same foreign vendor yields `id-100`, and an inconsistent answer is downgraded to `id-101` (INFO, worded to say **it accuses no one of a swap**).
4. **An ignored parameter may just be a compatibility-layer defect**, not necessarily malice. The report distinguishes these (`params-100` MEDIUM / `params-101` INFO).
5. **Some models may not support certain capabilities** (e.g. `logprobs`, `tools`). Probes record "explicit error" separately from "silently ignored" — the former is a compatibility gap, the latter is deception.
6. **Requests consume your credit.** The full probe set is roughly 210 requests across 6 models. Start with `--probes echo,tokenizer,twins` — `echo` costs two requests per model and is the cheapest hard check in the box.
7. **Rate limiting may prevent some probes from completing.** Probes report "could not run" honestly and never treat a crash as a pass.
8. **Reasoning models write hidden reasoning first and visible text second.** With too small a `max_tokens`, the visible content comes back empty, `finish_reason` is `length`, and `reasoning_content` holds a long block. **An empty reply is not evidence**: probes retry once with a larger budget before comparing anything, and a check that still has nothing to judge is recorded as "not checked" (`params-103` / `stream-103` / `*-000`) — never as "the parameter was ignored" or "the two paths disagree". This one was added after being burned: on a real, honest relay this exact shape produced one false HIGH and one false MEDIUM.
9. **An unreproducible station cannot be accused by comparison.** Some backends (MoE routing, batching) are non-deterministic even at `temperature=0`: the same request twice returns two different texts. In that situation "the streamed answer differs from the plain answer" and "two identical calls differed" are both **what noise looks like**. Probes measure the floor first; failing that they fall back to a deterministic question, and if even that cannot be judged they report `stream-104` / `params-104` — both INFO, and both worded to say **they accuse no one**. Writing that noise up as HIGH or MEDIUM is the same error in two different disguises. This one also comes from that real, honest station: it is what we hit immediately after fixing the reasoning-model empty replies.
10. **`ctx-clean` is only a lower bound.** It proves that input arrived intact up to the deepest rung tested, not that the advertised context length holds. Raise `--context-sizes` to go deeper — at the cost of longer, more expensive requests.
11. **The `model` field in a response is not testimony.** The station fills it in: it can be forged, rewritten into the sales name, or left empty. So a cross-vendor conflict is only *likely* (`echo-100`), and it is one of three mutually independent observations — read it alongside `twins` and `tokenizer`. The real value of this probe points the other way: because it exists, `echo-clean` is emitted only when **both** paths produced a comparable answer. Anything less is `echo-000` INFO.

---

## Development

```bash
# Self-test: local mock relays, eight scenarios
python tests/test_mock_relay.py
```

`tests/mock_relay.py` starts eight local servers:

- `fraudulent` — 4 model names served by 2 backends: cross-vendor aliasing, all parameters
  ignored, inconsistent streaming, hidden reasoning billed, and **long inputs cut down to the
  last 20,000 characters** (roughly 5k tokens, which lands between the context probe's first two
  default rungs, so a run shows the unambiguous shape of a shallow depth passing while a deeper
  one loses a marker)
- `clean` — 3 genuinely different tokenizers, honest self-reports, exact billing, all
  parameters honoured, long inputs forwarded whole
- `same-vendor` — two names of the same vendor on one backend (the legitimate-alias case),
  everything else honest
- `slow` — behaviour entirely correct, but every request takes 2–3 seconds (validates the
  probe time budget)
- `dead` — panel and model list fine, but **every completion fails** (validates the hard rule above)
- `reasoning` — a **reasoning-model backend**: with a small `max_tokens` the visible content is
  empty, `reasoning_content` is populated, `finish_reason` is `length`, and the bill honestly
  includes the reasoning tokens. Everything else about it is honest. This scenario guards the
  third kind of mistake — the one actually hit against a real station: **two empty strings must
  not be read as "the two paths disagree", and an empty sample must not be read as "the parameter
  was ignored"**
- `noisy` — an **honest relay whose backend cannot reproduce itself**: the same request at
  `temperature=0` twice in a row returns two different answers. Nothing is dropped and nothing is
  swapped; the noise floor is simply high. This scenario guards the fourth kind of mistake, the
  second one actually hit against that same real station: **when the station cannot repeat itself,
  neither "the two paths disagree" nor "two calls at temperature=0 differed" is evidence of
  anything**.
- `unstable-self` — a relay where the model **names a different vendor every time it is asked**:
  the same identity question twice yields two different answers. Everything else is honest. This
  scenario guards the fifth kind of mistake: **a self-report the model cannot even repeat is not a
  lead**, so it must be downgraded to `id-101` (INFO) instead of producing an `id-100`.

**Acceptance is two halves**: `fraudulent` must be caught (≥1 CRITICAL, and it must be a
specific finding id), and `clean` / `same-vendor` / `slow` / `dead` / `reasoning` / `noisy` /
`unstable-self` must produce **zero false positives** (`same-vendor` is allowed one LOW alias
notice). A tool that
cries wolf on honest relays is worse than no tool — it turns one real accusation into noise.
"Slow" is not "broken", your own timeout must never become an accusation against someone else,
and our token budget is not evidence about their parameters.

"Zero false positives" is not a slogan, it is a regression test:

- `test_same_vendor_aliases_are_not_accused` asserts that the `same-vendor` scenario produces
  no MEDIUM-or-above finding, that `tok-100` and `twins-100` are absent, and that `tok-101`
  and `twins-101` are present. Any change that re-escalates "same vendor sharing a tokenizer"
  into an accusation fails on the spot.
- `test_dead_relay_is_never_reported_as_clean` asserts that the `dead` scenario may not contain
  any of `id-clean` / `canary-clean` / `stream-clean` / `params-clean` / `twins-clean` /
  `ctx-clean`, and must contain `id-000` / `canary-000` / `stream-000` / `ctx-000` / `rel-100`.
  Any change that rewrites "could not check" back into "checked, no problem" fails on the spot.
- `test_context_probe_catches_silent_truncation` asserts that the `fraudulent` scenario reaches
  `ctx-100` at `HIGH` (a shallower control depth passed), with `missing == "head"` and the
  server-reported `prompt_tokens` present in the evidence; and that the `clean` scenario really
  produces `ctx-clean`. Without that second half the probe could rot into always emitting
  `ctx-000`, and every clean report would silently become meaningless.
- `test_reasoning_relay_is_not_falsely_accused` asserts that the `reasoning` scenario produces
  **nothing above CLEAN**, in particular neither `stream-100` nor `params-100`; and that
  `twins-clean` / `id-clean` / `canary-clean` really do appear. Not producing false positives is
  not enough — once the budget grows, the probes must actually reach a verdict instead of
  collectively degrading into `*-000`. This scenario comes from a real false positive: two
  reasoning models on an honest relay, and small-budget probes that mistook an empty reply for
  evidence.
- `test_noisy_relay_is_not_falsely_accused` asserts that the `noisy` scenario produces
  **nothing above CLEAN**, in particular neither `stream-100` nor `params-100`; that `stream-104`
  and `params-104` really do appear (silence would be its own lie — the station cannot reproduce
  itself and the report has to say so); and that `stream-clean` does **not** appear, because the
  open-ended comparison was discarded and claiming "streamed and plain agreed" would dress up a
  comparison that never ran as a pass.
- `test_echo_probe_compares_the_reported_model_name` asserts that `echo` alone on the
  `fraudulent` scenario still reaches `echo-100` (MEDIUM / likely), that the evidence names exactly
  those two cross-vendor aliases, and that **both** paths (plain and streaming) caught it; that
  `clean` yields only `echo-clean`; and that `same-vendor` yields `echo-101` with no `echo-100`.
  Any change that turns "one backend wearing two names" back into a cross-vendor accusation fails
  on the spot.
- `test_unstable_self_report_is_not_an_accusation` asserts that the `unstable-self` scenario
  produces **nothing above CLEAN**, no `id-100`, an `id-101`, and no `id-clean` either — that last
  half guards silence in the other direction: if the self-report is unstable, the report may not
  also claim "no contradiction found".
- `test_cli_survives_a_legacy_console_encoding` runs the real CLI in a child process with
  stdout pinned to cp936 (the default code page on Chinese Windows) and asserts that it finishes,
  writes its report, and exits 0. This one guards a different flavour of confusion: the progress
  line contained a glyph cp936 cannot encode (`✓`), which raised `UnicodeEncodeError` **inside the
  probe loop** — so a crash ended with status 1, the exact code reserved for "findings at or above
  `--fail-on`". From a shell, a typography bug and a real accusation looked identical.
- `test_model_autodiscovery_runs_without_models_flag` asserts that **without `--models`** — the path
  a first-time user takes — the models really are drawn from `/v1/models` for vendor diversity: the
  mock advertises three models from three vendors, so `--max-models 2` must take one of each rather
  than the first two catalogue entries. This one guards a **silent** failure: picking the wrong
  models raises no error, the probes end up comparing one backend against itself, and the report
  says the relay looks clean. A wrong selection is the only failure class with no visible symptom.

```bash
python tests/mock_relay.py --port 8123 --scenario fraudulent --verbose
```

### Project layout

```
relaycheck/
  client.py        HTTP client (retries, backoff, deadline, SSE parsing, base_url normalisation)
  models.py        Finding / Severity / Confidence / Usage / Completion
  families.py      Vendor keywords → family resolution (one copy shared by selection / echo / tokenizer / identity)
  selection.py     Pick models from /v1/models for vendor diversity
  reporter.py      Text / Markdown / JSON reports
  cli.py           Command-line entry point
  probes/
    base.py        Probe framework (crash isolation + wall-clock budget + progress heartbeats)
    reliability.py Reliability: success rate / latency percentiles (retries off, raw values)
    echo.py        The model name the response reports (once plain, once streaming)
    tokenizer.py   Tokenizer fingerprint
    twins.py       Twin behavioural comparison
    identity.py    Identity self-report + canary injection
    billing.py     Hidden reasoning billing + panel billing basis
    params.py      Parameter passthrough (7 behavioural tests)
    stream.py      Stream integrity
    context.py     Whether a long input is silently truncated (head/tail markers + ascending rungs)
tests/
  mock_relay.py        Mock relays (eight scenarios)
  test_mock_relay.py   End-to-end acceptance (15 tests)
  test_selection.py    Model selection units (15 tests, no network, no server)
examples/
  report-*.md          Four real tool outputs (swap / honest / dead / not reproducible)
.github/workflows/
  ci.yml               3.9 / 3.11 / 3.13 on Linux, plus one Windows and one macOS leg
SECURITY.md            Security boundaries, what a report does and does not contain, how to report
CHANGELOG.md           Behavioural changes, especially finding-id and severity semantics
```

> `families.py` is a separate file for a reason: `selection.py` (picking cross-vendor models),
> `tokenizer.py` (deciding whether a shared tokenizer is suspicious) and `identity.py`
> (comparing self-reports) must all give the **same answer** to "which vendor does this name
> belong to". Three independently maintained keyword tables will drift eventually, and then one
> model gets judged as two different vendors by two probes in the same report — the hardest
> class of false accusation to track down.

Adding a probe: subclass `Probe` from `probes/base.py`, implement `run(ctx)`, and register it in
`ALL_PROBES` in `probes/__init__.py`. **If your probe loops over requests, you must check
`ctx.out_of_budget()` inside the loop, catch `RelayBudgetExceeded`, and call
`self._note_budget(...)`** — otherwise one slow relay turns your probe into a black box that
never stops. `RelayBudgetExceeded` must be caught **separately**, never inside a blanket
`except Exception`: that would turn "we stopped it ourselves" into "the relay errored", i.e. a
false accusation manufactured by our own timeout.

---

## Security

Read [`SECURITY.md`](SECURITY.md) before you aim this at anything. Three points:

- **Use your own key, against a station you already pay for.** The tool sends read-only chat
  requests: it posts no messages, changes no settings, and performs no writes against billing
  endpoints. Its only side effect is that **those requests are genuinely billed to your account**.
- **Your API key never reaches a report.** `relaycheck/reporter.py` contains no reference to
  `api_key`; reports carry the target URL and nothing else about your credential. But they do
  contain **your** account's balance, spend and usage history — publicise a report and you
  publicise your bill. Strip the panel/billing sections before sharing one.
- **The output is evidence, not a verdict.** `severity` (how bad) and `confidence` (how sure) are
  deliberately separate; `LOW` / `SUSPECTED` means "a lead that cannot carry an accusation". Read
  "Honest limitations" and each finding's remediation before you take a report to a merchant.

(The full policy, including how to report a vulnerability in relaycheck itself, is currently
written in Chinese; an English translation is welcome.)

---

## License

MIT
