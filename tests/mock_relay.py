"""Local mock relays: one fraudulent, one honest, and six edge cases.

This exists so ``relaycheck`` can be validated end to end without touching
anyone's production service. Eight scenarios:

``fraudulent``
    Four model names served by two backends. Tokenizers are shared across
    supposedly-different vendors, outputs are byte-identical across those pairs,
    self-identification contradicts the sold name, chain-of-thought is billed but
    hidden, parameters are silently ignored, the stream returns a different
    (shorter) answer than the non-streaming path with no usage block, and a long
    prompt is quietly trimmed to its last few thousand characters before the
    backend ever sees it — while the full prompt is still what gets billed.

``clean``
    Three genuinely different tokenizers, honest self-identification, exact
    token accounting, parameters honoured, stream identical to non-stream.

``same-vendor``
    Two names from one vendor over one backend. Honest in every other respect.
    This is the shape a *legitimate* alias pair takes, so the detector must not
    accuse it: at most a LOW note.

``slow``
    Behaves correctly but takes seconds per request. Guards the probe time
    budget: a slow relay must end in "incomplete", never in a hang and never in
    an accusation built from our own timeout.

``dead``
    Panel and key check work; every completion fails. Guards the inverse
    mistake: "we got no answer" must never be reported as CLEAN.

``reasoning``
    An honest relay whose backend is a reasoning model, like every DeepSeek
    reseller on the market today. Ask for a small ``max_tokens`` and the budget
    is spent on hidden reasoning: ``content`` comes back empty and
    ``finish_reason`` is ``"length"``. Guards the third mistake, the one that
    actually happened against a real honest station: two empty answers must not
    be read as "the two paths disagree", and an empty sample must not be read as
    "the parameter was ignored". Nothing about this relay is dishonest, so the
    only acceptable verdict is a report with nothing above INFO.

``noisy``
    An honest relay whose backend is not reproducible: the same request at
    ``temperature=0`` twice in a row yields two different answers. This is
    normal for a batched or mixture-of-experts backend, and it is the second
    mistake that actually happened against a real honest station. When the
    station cannot repeat itself, "the streamed answer differs from the plain
    answer" proves nothing, and neither does "two calls at temperature=0
    differed" — the noise floor has to be measured before either comparison is
    allowed to become an accusation. Deterministic questions (which have exactly
    one correct answer) still work, so the probes keep a usable fallback.

``unstable-self``
    An honest relay with nothing swapped, whose backend will not give the same
    answer twice about its own origin. Ask a thinking model "which company made
    you?" and the first reply is a sample, not a fact — a real station produced
    "sold as anthropic, says openai", and thirty seconds later said something
    else again. The self-report is therefore checked twice before it is allowed
    to become a lead, and this scenario is the guard on that: the report must say
    "this lead does not hold" instead of printing an accusation.

A detector that flags the clean relay is broken. A detector that reports CLEAN
on the dead relay is worse — it launders a dead endpoint into a clean report.
A detector that accuses the reasoning relay of swapping models is worse still:
it turns the most common honest deployment on the market into a false
accusation. A detector that accuses the noisy relay of either swapping models
or dropping parameters is the same mistake wearing a different hat, and a
detector that turns one wandering self-description into an accusation is that
mistake a third time. All eight scenarios are exercised by
``tests/test_mock_relay.py``.

Run standalone::

    python tests/mock_relay.py --port 8123 --scenario fraudulent
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TEST_API_KEY = "sk-relaycheck-local-test"

#: How much of a long prompt the fraudulent relay actually forwards upstream.
#: Roughly 5k tokens, which sits between the ``context`` probe's first two
#: default rungs (2000 and 8000) — so the probe sees a passing shallow depth and
#: a failing deep one, which is the unambiguous shape of a silent truncation.
_TRUNCATE_CHARS = 20000

#: Tokens a reasoning backend spends on hidden ``reasoning_content`` before it
#: emits a single visible character. Modelled on a real DeepSeek-style reasoning
#: model, which answers "say hello in five words" with 16 reasoning tokens and
#: an *empty* ``content`` when ``max_tokens=16``.
#:
#: The ``reasoning`` scenario exists because this shape broke relaycheck: two
#: empty answers compared as "different", and an honest relay that merely serves
#: reasoning models got a HIGH-severity accusation of swapping models behind the
#: stream. 700 sits above every small budget the probes used to ask for
#: (twins 200, temperature 120, stop 64, canary 60, context 128) and below the
#: grown budgets they now fall back to (1024+), so the scenario exercises both
#: the failure and its fix.
_REASONING_TOKENS = 700

#: Stand-in text for ``reasoning_content``. Never fed to :func:`_answer`, so it
#: cannot collide with any other mock branch.
_REASONING_SAMPLE = (
    "The user is asking for something short. I should answer directly and "
    "keep the wording plain. Let me check the request once more before "
    "committing to an answer. "
)

# --------------------------------------------------------------------- models

#: name -> (tokenizer variant, claimed family, honest self-description, true vendor)
FRAUDULENT_MODELS: dict[str, tuple[str, str, str]] = {
    # Two real backends, four names, cross-vendor aliasing.
    "gpt-4o": ("narrow", "OpenAI", "I was created by OpenAI."),
    "gemini-1.5-pro": ("narrow", "Google", "I was created by OpenAI."),
    "claude-3-5-sonnet": ("wide", "Anthropic", "I was created by Anthropic."),
    "deepseek-chat": ("wide", "DeepSeek", "I was developed by MiniMax."),
}

CLEAN_MODELS: dict[str, tuple[str, str, str]] = {
    "gpt-4o": ("narrow", "OpenAI", "I was created by OpenAI."),
    "claude-3-5-sonnet": ("wide", "Anthropic", "I was created by Anthropic."),
    "deepseek-chat": ("cjk", "DeepSeek", "I was created by DeepSeek."),
}

#: Two names that claim the *same* vendor and are served by one shared backend.
#: This is the false-positive trap: an honest relay exposing one vendor's lineup
#: (or two labels over the same weights, like ``gpt-4o`` / ``chatgpt-4o-latest``)
#: looks exactly like this, so the audit must NOT accuse it of substitution.
SAME_VENDOR_MODELS: dict[str, tuple[str, str, str]] = {
    "gpt-4o": ("narrow", "OpenAI", "I was created by OpenAI."),
    "gpt-4o-mini": ("narrow", "OpenAI", "I was created by OpenAI."),
}

#: Sales name -> the name of the backend that is actually answering. The mock
#: fills the response ``model`` field from here, the way a real relay does when it
#: fronts one set of weights with several names.
#:
#: This is deliberately keyed by tokenizer *variant*, not per model name, so that
#: aliases of one backend really do answer under one name — which is what makes
#: the ``clean`` scenario produce ``echo-clean`` (each name maps to itself) while
#: ``fraudulent`` produces a cross-vendor mismatch on both of its pairs.
_VARIANT_BACKEND: dict[str, str] = {
    "narrow": "gpt-4o",
    "wide": "claude-3-5-sonnet",
    "cjk": "deepseek-chat",
}

_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]|[^\sA-Za-z0-9_]")


def count_tokens(text: str, variant: str) -> int:
    """Deterministic stand-in for a real tokenizer."""
    base = len(_WORD_RE.findall(text))
    if variant == "wide":
        return int(base * 1.6) + len(text) // 8
    if variant == "cjk":
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return base + cjk * 2
    return base


# --------------------------------------------------------------- http handler


class _State:
    def __init__(self, scenario: str, api_key: str, verbose: bool = False) -> None:
        self.scenario = scenario
        self.api_key = api_key
        self.verbose = verbose
        self.models = {
            "fraudulent": FRAUDULENT_MODELS,
            "same-vendor": SAME_VENDOR_MODELS,
        }.get(scenario, CLEAN_MODELS)
        #: The ``slow`` scenario models a relay that answers correctly but takes
        #: forever. It exists to prove that the probe time budget turns "hangs for
        #: 40 minutes" into "stops early and says it is incomplete".
        self.delay_s = 3.0 if scenario == "slow" else 0.0
        #: The ``dead`` scenario models a relay whose panel and key check work
        #: but whose gateway answers nothing. Every chat request fails. It exists
        #: to prove that probes which cannot measure report "unchecked" instead of
        #: CLEAN — "we got no answer" is not "the answer looked fine".
        self.dead = scenario == "dead"
        #: The ``reasoning`` scenario models an honest relay whose backend is a
        #: reasoning model. With too small a ``max_tokens`` it returns hidden
        #: reasoning and *no* visible text. Nothing about it is fraudulent — it
        #: exists to prove relaycheck does not mistake "empty" for "different".
        self.reasoning = scenario == "reasoning"
        #: The ``noisy`` scenario models an honest relay whose backend cannot
        #: repeat itself: the same request at ``temperature=0`` twice gives two
        #: different answers. Nothing is dropped and nothing is swapped — the
        #: noise floor is simply high. It exists to prove relaycheck measures
        #: that noise before it accuses anyone of either swapping models or
        #: dropping sampling parameters.
        self.noisy = scenario == "noisy"
        #: The ``unstable-self`` scenario models an honest relay — right models,
        #: right backend, nothing swapped — whose backend will not give the same
        #: answer twice about its own origin. A thinking model does this: the
        #: first answer is a sample, not a fact. It exists to prove that a
        #: wandering self-report is reported as "this lead does not hold" instead
        #: of being upgraded into an accusation of substitution.
        self.unstable_self = scenario == "unstable-self"
        #: How many times each identity question has been asked, so the mocked
        #: witness can contradict itself *per question* rather than per request.
        #: A per-request rotation does not work here: the re-ask of one question
        #: and the re-ask of the next are a different number of requests apart,
        #: so no fixed period makes both pairs disagree.
        self._self_asks: dict[str, int] = {}

    def next_self_report(self, key: str) -> str:
        """A different claimed origin each time the same question is asked."""
        seen = self._self_asks.get(key, 0)
        self._self_asks[key] = seen + 1
        return _UNSTABLE_SELF_REPORTS[seen % len(_UNSTABLE_SELF_REPORTS)]

    @property
    def fraudulent(self) -> bool:
        return self.scenario == "fraudulent"

    @property
    def truncates(self) -> bool:
        """Whether the upstream backend sees only the tail of the prompt.

        The fraudulent relay quietly drops everything before the last
        :data:`_TRUNCATE_CHARS` characters, then bills the full prompt. This is
        what the ``context`` probe is built to catch, and it is modelled here the
        same way a real relay does it: no error, no warning, a perfectly fluent
        answer.
        """
        return self.scenario == "fraudulent"

    @property
    def answer_mode(self) -> str:
        """How :func:`_answer` should produce text.

        ``fraudulent``
            One shared backend, several names, different claimed vendors — and
            every tuning parameter silently dropped.
        ``aliased``
            One shared backend, several names, the *same* claimed vendor.
            Everything else is honest, which is the whole point: this scenario
            exists to make sure a legitimate alias pair is not accused.
        ``honest``
            Distinct backends, honest parameter handling.
        """
        return {"fraudulent": "fraudulent", "same-vendor": "aliased"}.get(
            self.scenario, "honest"
        )


def _make_handler(state: _State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "mock-relay/1.0"

        # ---------------------------------------------------------- utilities

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            if state.verbose:
                print(f"[mock-relay] {self.address_string()} {fmt % args}", flush=True)

        def _maybe_delay(self) -> None:
            if state.delay_s:
                time.sleep(state.delay_s)

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:  # noqa: BLE001
                return {}

        def _authorised(self) -> bool:
            auth = self.headers.get("Authorization") or ""
            return auth == f"Bearer {state.api_key}"

        # -------------------------------------------------------------- routes

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]

            if path == "/health":
                self._send_json(200, {"status": "ok"})
                return

            if not self._authorised():
                self._send_json(401, {"code": "INVALID_API_KEY", "message": "Invalid API key"})
                return

            if path in ("/v1/models", "/models"):
                now = int(time.time())
                self._send_json(200, {
                    "object": "list",
                    "data": [
                        {"id": name, "object": "model", "created": now, "owned_by": "mock"}
                        for name in state.models
                    ],
                })
                return

            if path == "/v1/sub2api/billing":
                self._send_json(200, {
                    "object": "sub2api.key_billing",
                    "schema_version": 1,
                    "billing_scope": "token",
                    "group_rate_multiplier": 0.32,
                    "resolved_rate_multiplier": 0.32,
                    "peak_rate_enabled": False,
                    "effective_rate_multiplier": 0.32,
                })
                return

            if path == "/v1/usage":
                if state.fraudulent:
                    stats = [
                        {"model": "deepseek-chat", "requests": 107,
                         "cost": 0.9844897, "account_cost": 0.047444091},
                        {"model": "gpt-4o", "requests": 73,
                         "cost": 0.4544709, "account_cost": 0.4544709},
                    ]
                else:
                    stats = [
                        {"model": "gpt-4o", "requests": 10, "cost": 0.01},
                    ]
                self._send_json(200, {
                    "balance": 19.49329354,
                    "remaining": 19.49329354,
                    "planName": "钱包余额",
                    "mode": "unrestricted",
                    "model_stats": stats,
                })
                return

            if path == "/api/v1/settings/public":
                self._send_json(200, {
                    "site_name": "Mock Relay",
                    "channel_monitor_enabled": False,
                    "allow_user_view_error_requests": False,
                })
                return

            if path == "/api/v1/model-plaza":
                self._send_json(200, {"groups": [{"id": 1, "name": "default", "rate_multiplier": 0.32}]})
                return

            if path == "/api/v1/setup/status":
                self._send_json(200, {"needs_setup": False})
                return

            self._send_json(404, {"error": {"message": f"no route {path}"}})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            # Always drain the request body, including on the paths that answer
            # without looking at it. A large body left unread desynchronises a
            # keep-alive connection — the leftover bytes get parsed as the next
            # request line and the client sees a bogus ``400 Bad Request`` HTML
            # page instead of the answer we actually sent. The ``context`` probe
            # is the first one to send bodies big enough to expose this.
            body = self._read_json()
            if path not in ("/v1/chat/completions", "/chat/completions"):
                self._send_json(404, {"error": {"message": f"no route {path}"}})
                return
            if not self._authorised():
                self._send_json(401, {"code": "INVALID_API_KEY", "message": "Invalid API key"})
                return
            if state.dead:
                # The panel is up and the key is valid, but the gateway cannot
                # serve a single completion. This is the "everything is broken"
                # case that used to make the identity/canary probes report CLEAN
                # because they mistook "no answer" for "answer looked fine".
                self._send_json(
                    503,
                    {"error": {"message": "upstream temporarily unavailable", "code": "upstream_error"}},
                )
                return

            model = str(body.get("model") or "")
            if model not in state.models:
                self._send_json(404, {
                    "error": {"message": f"The model `{model}` does not exist", "code": "model_not_found"}
                })
                return

            self._maybe_delay()
            variant, _claimed, honest_self = state.models[model]
            # What the backend calls itself. A relay that fronts several sales
            # names with one backend has to fill in this field somehow, and the
            # honest answer is the name it actually asked — not the name you
            # bought. ``_VARIANT_BACKEND`` makes the mock behave that way, which
            # is what gives the ``echo`` probe something real to compare.
            backend_model = _VARIANT_BACKEND[variant]
            messages = body.get("messages") or []
            prompt = _flatten(messages)
            if state.unstable_self:
                identity_key = _identity_key(prompt)
                if identity_key:
                    honest_self = state.next_self_report(identity_key)
            # What the backend actually gets to read. The fraudulent relay trims
            # the head and says nothing about it; ``_usage`` below still bills the
            # full prompt, which is the part of the trick that costs you money.
            seen = prompt[-_TRUNCATE_CHARS:] if state.truncates else prompt
            answer = _answer(
                model,
                variant,
                seen,
                honest_self,
                state.answer_mode,
                body.get("temperature"),
                state.noisy,
            )

            # A reasoning backend thinks before it speaks. Given enough room it
            # returns the reasoning *and* the visible text; given too little, the
            # whole budget is consumed by the reasoning alone, ``content`` comes
            # back as an empty string, and ``finish_reason`` is ``"length"``.
            # Nothing here is fraudulent — which is exactly why relaycheck must
            # not read the empty answer as evidence of anything.
            reasoning: dict[str, Any] | None = None
            finish_reason = "stop"
            if state.reasoning:
                budget = body.get("max_tokens")
                budget = int(budget) if isinstance(budget, (int, float)) else None
                starved = budget is not None and budget <= _REASONING_TOKENS
                reasoning = {
                    "reasoning_content": _REASONING_SAMPLE * 6,
                    "reasoning_tokens": budget if starved else _REASONING_TOKENS,
                }
                if starved:
                    answer = ""
                    finish_reason = "length"

            usage = _usage(
                prompt,
                answer,
                variant,
                state.fraudulent,
                body,
                reasoning_tokens=reasoning["reasoning_tokens"] if reasoning else 0,
            )

            if body.get("stream"):
                self._stream(model, answer, usage, body, backend_model)
                return

            choices = [
                _choice(
                    answer,
                    body,
                    state.fraudulent,
                    finish_reason=finish_reason,
                    reasoning_content=reasoning["reasoning_content"] if reasoning else None,
                )
            ]
            if body.get("n") and int(body["n"]) > 1 and not state.fraudulent:
                choices.append(
                    _choice(
                        answer,
                        body,
                        state.fraudulent,
                        finish_reason=finish_reason,
                        reasoning_content=reasoning["reasoning_content"] if reasoning else None,
                    )
                )

            payload: dict[str, Any] = {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": backend_model,
                "choices": choices,
                "usage": usage,
            }
            if body.get("tools") and not state.fraudulent:
                # Honest relay: actually route the tool call.
                tools = body.get("tools") or []
                name = "get_weather"
                if isinstance(tools, list) and tools:
                    fn = (tools[0] or {}).get("function") or {}
                    name = fn.get("name") or name
                choices[0]["message"]["tool_calls"] = [{
                    "id": "call_mock_1",
                    "type": "function",
                    "function": {"name": name, "arguments": '{"city": "Reykjavik"}'},
                }]
                choices[0]["finish_reason"] = "tool_calls"

            if body.get("logprobs") and not state.fraudulent:
                choices[0]["logprobs"] = {
                    "content": [{"token": "mock", "logprob": -0.01, "top_logprobs": []}]
                }
            self._send_json(200, payload)

        # ------------------------------------------------------------- helpers

        def _stream(
            self,
            model: str,
            answer: str,
            usage: dict[str, Any],
            body: dict[str, Any],
            backend_model: str | None = None,
        ) -> None:
            """Server-sent events. Fraudulent mode diverges from the plain path."""
            reported = backend_model or model
            if state.fraudulent:
                # A short canned prefix, no usage block, and no relation to the
                # answer the non-streaming path would have returned.
                text = "OK."
                send_usage = False
            else:
                text = answer
                send_usage = bool((body.get("stream_options") or {}).get("include_usage"))

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def emit(obj: dict[str, Any]) -> None:
                self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()

            for piece in _chunks(text, 12):
                emit({
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": reported,
                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                })
                if not state.fraudulent:
                    time.sleep(0.02)  # real incremental generation

            emit({
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": reported,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            })
            if send_usage:
                emit({
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": reported,
                    "choices": [],
                    "usage": usage,
                })
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    return Handler


# ------------------------------------------------------------------- patterns


_CONTEXT_CODE_RE = re.compile(r"\b(ALPHA|BETA)-([A-Z2-9]{12})\b")


def _context_codes(prompt: str) -> str | None:
    """Answer the ``context`` probe's question from whichever codes are present.

    A relay that forwarded the whole prompt has both codes; one that quietly
    trimmed the head has only the tail code, and returning just that one is
    exactly the evidence the probe is looking for. ``None`` means the prompt was
    not a context-probe prompt at all, so normal answering continues.
    """
    found = _CONTEXT_CODE_RE.findall(prompt)
    if not found:
        return None
    return ",".join(f"{label}-{code}" for label, code in found[:2])


def _flatten(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
    return "\n".join(parts)


#: What the ``unstable-self`` witness says, in order, the first, second, ...
#: time it is asked the *same* question. The first entry names a vendor that
#: does not match any model in :data:`CLEAN_MODELS`, so the first round really
#: does look like a contradiction; the second round does not repeat it.
_UNSTABLE_SELF_REPORTS: tuple[str, ...] = (
    "I was created by Anthropic.",
    "I was created by OpenAI.",
    "I was created by Google.",
)


def _identity_key(prompt: str) -> str | None:
    """Which identity question this is, in the terms :func:`_answer` uses.

    Mirrors the guards inside :func:`_answer` so the unstable witness rotates
    per *question* and not per request.
    """
    lowered = prompt.lower()
    if "created you" in lowered or "who" in lowered:
        return "creator"
    if "base model" in lowered:
        return "pivot"
    return None


def _answer(
    model: str,
    variant: str,
    prompt: str,
    honest_self: str,
    mode: str,
    temperature: Any = None,
    noisy: bool = False,
) -> str:
    """Deterministic answer, keyed on ``mode`` (see ``_State.answer_mode``).

    The ordering matters. The special-cased prompts (canary echo, digit runs,
    JSON, sampling) are handled the *same way* for ``honest`` and ``aliased`` —
    the only difference between those two modes is the fallback, which picks
    between a name-dependent and a name-independent string. Getting that wrong
    is how the ``same-vendor`` scenario ended up looking like a relay that
    ignores ``temperature`` and ``response_format``.

    ``noisy`` models an honest backend that cannot repeat itself: only the
    open-ended fallback varies between calls, so a question with exactly one
    correct answer still gets that answer. That asymmetry is the whole point —
    it is what lets a probe tell "the station is noisy" apart from "the station
    is serving two different models".

    ``prompt`` is what the *backend* sees, which is not necessarily what the
    caller sent: see ``_State.truncates``.
    """
    # Labelled codes from the context probe. Handled first and for every mode,
    # because what this replies reports is which part of the prompt arrived —
    # a question that is independent of how honest the relay is about anything
    # else. On the fraudulent scenario only the tail code is still present.
    codes = _context_codes(prompt)
    if codes is not None:
        return codes

    lowered = prompt.lower()
    if "who" in lowered or "created you" in lowered or "base model" in lowered:
        return honest_self

    if mode == "fraudulent":
        # Deterministic per (variant, prompt), deliberately independent of the
        # model name, and blind to every tuning parameter — that is the fraud.
        digest = sum(ord(c) * (i + 1) for i, c in enumerate(prompt)) % 9973
        return (
            f"[{variant}] Answer for digest {digest}: "
            "the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; "
            '{"tool":"relaycheck","value":"relaycheck","n":7}; '
            "the result is 121.0; PORT=22 ssh, PORT=53 domain, PORT=80 http, "
            "PORT=443 https, PORT=3306 mysql."
        )

    # Honest answers, shared by the ``honest`` and ``aliased`` modes.
    def prose(text: str) -> str:
        """Free-form prose, as an unreproducible backend would return it.

        Only the prose branches go through here. The answers that have exactly
        one right value (the deterministic arithmetic, the canary echo, the JSON
        object, the digit runs) stay byte-stable even on the ``noisy`` scenario,
        because that asymmetry is the real-world one: a station can be unable to
        repeat a paragraph while still being able to answer ``37 * 41``.
        """
        if not noisy:
            return text
        return f"{text} (draft {random.randint(100000, 999999)})"

    canary = re.search(r"RC-[A-Z2-9]{12}", prompt)
    if canary and "repeat" in lowered:
        # A relay that forwards the prompt faithfully lets the model echo it.
        return canary.group(0)
    if "1 to 20" in prompt or "1 to 400" in prompt:
        return " ".join(str(i) for i in range(1, 21))
    if "digits 0 through 9" in prompt:
        return "0 1 2 3 4 5 6 7 8 9"
    if "17 * 23" in prompt or "(17" in prompt:
        return "121"
    if "37 * 41" in prompt:
        # One question, one answer. Even the ``noisy`` backend gets this right,
        # which is exactly why the stream probe falls back to it: sampling noise
        # cannot explain 1517 turning into something else.
        return "1517"
    if "TCP" in prompt or "ports" in prompt:
        return prose(
            "PORT=22 ssh\nPORT=53 domain\nPORT=80 http\nPORT=443 https\nPORT=3306 mysql"
        )
    try:
        hot = temperature is not None and float(temperature) > 0.5
    except (TypeError, ValueError):
        hot = False
    if hot:
        # An honest sampler actually samples: the same prompt must not come back
        # byte-identical at a high temperature.
        return (
            f"{model} draws {random.randint(10**6, 10**7 - 1)}: the harbour is "
            "built from compacted wind and everyone there speaks in tides."
        )
    if "JSON" in prompt or "json" in prompt:
        return '{"tool":"relaycheck","value":"relaycheck","n":7}'

    if mode == "aliased":
        # The fallback for an open-ended prompt. Keyed on (variant, prompt) and
        # NOT on the model name, so two labels over one backend look identical.
        digest = sum(ord(c) * (i + 1) for i, c in enumerate(prompt)) % 9973
        return (
            f"[{variant}] Answer for digest {digest}: "
            "the integers are 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; "
            '{"tool":"relaycheck","value":"relaycheck","n":7}; '
            "the result is 121.0; PORT=22 ssh, PORT=53 domain, PORT=80 http, "
            "PORT=443 https, PORT=3306 mysql."
        )
    if noisy:
        # An honest backend that cannot repeat itself. Nothing is dropped and
        # nothing is swapped — the same prompt at temperature=0 simply comes back
        # a different way every time. Modelled on a real station: two identical
        # requests returned two different paragraphs, which is what a batched or
        # mixture-of-experts backend does all day long.
        return prose(f"{model} says: {prompt[:40]}")
    return f"{model} says: {prompt[:40]}"


def _chunks(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _usage(
    prompt: str,
    answer: str,
    variant: str,
    fraudulent: bool,
    body: dict[str, Any],
    reasoning_tokens: int = 0,
) -> dict[str, Any]:
    prompt_tokens = count_tokens(prompt, variant) + 7  # template overhead
    visible = count_tokens(answer, variant)

    if fraudulent:
        max_tokens = body.get("max_tokens")
        if max_tokens:
            # max_tokens is accepted and ignored: always bill a long completion.
            completion = visible + 180
        else:
            completion = visible + 180
    else:
        # Hidden reasoning is billed truthfully by an honest relay — the tokens
        # were really spent, they just are not part of `content`.
        completion = visible + reasoning_tokens
        max_tokens = body.get("max_tokens")
        if max_tokens and completion > int(max_tokens):
            completion = int(max_tokens)

    payload = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion,
        "total_tokens": prompt_tokens + completion,
    }
    if reasoning_tokens:
        payload["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    return payload


def _choice(
    answer: str,
    body: dict[str, Any],
    fraudulent: bool = False,
    finish_reason: str = "stop",
    reasoning_content: str | None = None,
) -> dict[str, Any]:
    text = answer
    stop = body.get("stop")
    if stop and not fraudulent:
        # The fraudulent relay accepts `stop` and silently drops it.
        stops = [stop] if isinstance(stop, str) else list(stop)
        for token in stops:
            if token and token in text:
                idx = text.index(token)
                text = text[:idx]
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return {
        "index": 0,
        "message": message,
        "finish_reason": finish_reason,
    }


def _unsupported_noise() -> None:
    random.random()  # kept so the module seed is not accidentally constant


# ------------------------------------------------------------------------ main


class _MockRelayServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that stays quiet when a client walks away.

    The probes work under a wall-clock budget and deliberately abandon requests
    mid-flight when it runs out, which makes the socket layer raise
    ``ConnectionResetError`` inside the handler thread. That is the *expected*
    outcome of those runs, not a fault, so it must not spray tracebacks over the
    test output where a real failure would go unnoticed.
    """

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:  # type: ignore[override]
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def serve(
    port: int, scenario: str, api_key: str = TEST_API_KEY, verbose: bool = False
) -> ThreadingHTTPServer:
    state = _State(scenario, api_key, verbose)
    return _MockRelayServer(("127.0.0.1", port), _make_handler(state))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Local mock relay for relaycheck tests")
    p.add_argument("--port", type=int, default=8123)
    p.add_argument(
        "--scenario",
        choices=[
            "fraudulent",
            "clean",
            "slow",
            "same-vendor",
            "dead",
            "reasoning",
            "noisy",
            "unstable-self",
        ],
        default="fraudulent",
        help="slow = 行为正确但每次请求慢 3 秒，用于验证探针超时预算",
    )
    p.add_argument("--api-key", default=TEST_API_KEY)
    p.add_argument("--verbose", action="store_true", help="log every request")
    args = p.parse_args(argv)

    httpd = serve(args.port, args.scenario, args.api_key, verbose=args.verbose)
    print(f"mock-relay [{args.scenario}] listening on http://127.0.0.1:{args.port}", flush=True)
    print(f"api key: {args.api_key}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
