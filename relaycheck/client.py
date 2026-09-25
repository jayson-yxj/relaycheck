"""HTTP client for OpenAI-compatible relay endpoints.

Design notes
------------
* Many relays sit behind Cloudflare, which blocks scripted User-Agents with a
  ``403`` + ``error code: 1010``. We therefore send a browser UA by default.
* Relays are flaky in ways official APIs are not: origin timeouts (CF 522),
  truncated streams, empty ``usage`` blocks. Every call is retried with
  exponential backoff, and the number of *billable* attempts is tracked so the
  caller can keep the cost of an audit tiny.
* Streaming is first-class because "is the stream real?" is itself a probe.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

from .models import Completion, Usage

DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


def normalise_base_url(url: str) -> str:
    """Reduce whatever the user pasted to a scheme://host root.

    Every path used internally is already absolute from the origin
    (``/v1/chat/completions``, ``/api/v1/settings/public``), so a trailing
    version segment would otherwise be duplicated. Users habitually paste the
    full OpenAI-compatible endpoint, so accept all of these:

    * ``https://api.example.com``
    * ``https://api.example.com/``
    * ``https://api.example.com/v1``
    * ``https://api.example.com/v1/chat/completions``
    """
    cleaned = (url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/v1"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return cleaned.rstrip("/")


class RelayError(RuntimeError):
    """An HTTP or protocol-level failure talking to the relay."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body

    def describe(self, limit: int = 160) -> str:
        """One-line form for evidence blobs.

        Two things this must not do:

        1. A request that died before any response existed (DNS, TLS, connection
           reset, read timeout) has ``status is None``. Naive formatting renders
           that as ``"None: ..."``, which reads like a status code and buries the
           actual failure. Say the unhelpful truth instead of the misleading one.
        2. An error response carries the relay's own explanation in the body.
           ``"HTTP 402: ... returned 402"`` tells the reader nothing they could
           act on or verify; the upstream's ``{"error": {"message": ...}}`` usually
           names the real cause (quota, model gone, upstream down).
        """
        text = " ".join(str(self).split())
        body = " ".join((self.body or "").split())
        if body and body not in text:
            text = f"{text} — {body}"
        if len(text) > limit:
            text = text[: limit - 1] + "…"
        if self.status is None:
            return text
        return f"HTTP {self.status}: {text}"


class RelayBudgetExceeded(RelayError):
    """The current probe's wall-clock budget ran out mid-request.

    Distinct from a normal :class:`RelayError` on purpose: a probe must treat
    "the relay was too slow and we stopped" very differently from "the relay
    returned an error". Swallowing this into a generic failure handler would let
    a truncated probe look like a completed one.
    """


@dataclass
class StreamChunk:
    """One parsed SSE chunk, with arrival timing."""

    index: int
    t_offset_s: float
    text: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamResult:
    """Everything we learned from a streaming call."""

    model_requested: str
    model_returned: str | None
    text: str
    chunks: list[StreamChunk]
    usage: Usage
    finish_reason: str | None
    ttft_s: float | None          # time to first *content* token
    total_s: float
    http_status: int = 200

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def gaps(self) -> list[float]:
        """Inter-chunk arrival gaps, in seconds."""
        return [b.t_offset_s - a.t_offset_s for a, b in zip(self.chunks, self.chunks[1:])]

    def to_dict(self, include_text: bool = True) -> dict[str, Any]:
        d = {
            "model_requested": self.model_requested,
            "model_returned": self.model_returned,
            "chunk_count": self.chunk_count,
            "ttft_s": None if self.ttft_s is None else round(self.ttft_s, 3),
            "total_s": round(self.total_s, 3),
            "visible_chars": len(self.text),
            "finish_reason": self.finish_reason,
            "usage": self.usage.to_dict(),
            "http_status": self.http_status,
        }
        if include_text:
            d["text"] = self.text
        return d


class RelayClient:
    """Thin, explicit client. No SDK, no hidden magic."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 60.0,
        max_retries: int = 3,
        user_agent: str = DEFAULT_BROWSER_UA,
        extra_headers: dict[str, str] | None = None,
        delay_between_requests: float = 0.4,
        verbose: bool = False,
    ):
        self.base_url = normalise_base_url(base_url)
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.delay_between_requests = delay_between_requests
        self.verbose = verbose

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Authorization": f"Bearer {api_key}",
        })
        if extra_headers:
            self.session.headers.update(extra_headers)

        #: total HTTP requests actually sent (including retries)
        self.request_count = 0
        #: requests that plausibly reached the model and may have been billed
        self.billable_count = 0
        self._last_call = 0.0
        #: monotonic deadline for the current probe (0.0 = no deadline).
        #: Set by ``run_probes``; see :meth:`check_deadline`.
        self.deadline = 0.0

    # ---------------------------------------------------------------- deadline

    def check_deadline(self, *, what: str = "request") -> None:
        """Raise :class:`RelayBudgetExceeded` once the probe budget is spent.

        This is the safety net that keeps a flaky relay from turning a 30-request
        probe into a 30-minute hang: a single 45-second read timeout, retried
        three times, multiplied across dozens of requests.
        """
        if self.deadline and time.monotonic() >= self.deadline:
            raise RelayBudgetExceeded(
                f"probe time budget exhausted before {what}; the relay was too slow "
                "to finish this probe (raise --budget, or check --probes reliability)"
            )

    # ---------------------------------------------------------------- plumbing

    def _throttle(self) -> None:
        if self.delay_between_requests <= 0:
            return
        wait = self.delay_between_requests - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"    · {msg}", flush=True)

    def url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        stream: bool = False,
        billable: bool = False,
    ) -> requests.Response:
        """Send a request with retry/backoff. Raises :class:`RelayError` on failure."""
        url = self.url(path)
        last_err: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self.check_deadline(what=f"{method} {path}")
            self._throttle()
            self.request_count += 1
            if billable:
                self.billable_count += 1
            # Never let a single request run far past the probe deadline: the
            # whole point of the deadline is that "slow relay" degrades into
            # "partially checked" instead of "hung".
            timeout = self.timeout
            if self.deadline:
                remaining = self.deadline - time.monotonic()
                timeout = max(5.0, min(timeout, remaining))
            try:
                resp = self.session.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    stream=stream,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                last_err = exc
                self._log(f"{method} {path} -> {exc!r} (attempt {attempt + 1})")
            else:
                if resp.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                    retry_after = _retry_after_seconds(resp)
                    self._log(
                        f"{method} {path} -> {resp.status_code}, retrying in {retry_after:.1f}s"
                    )
                    resp.close()
                    time.sleep(retry_after)
                    continue
                return resp

            if attempt < self.max_retries:
                time.sleep(min(2.0 ** attempt, 8.0))

        raise RelayError(f"{method} {path} failed after retries: {last_err!r}", body=str(last_err))

    def get_json(self, path: str, **kw: Any) -> tuple[int, Any]:
        """GET a path and return ``(status, parsed_json_or_text)``. Never raises on 4xx."""
        resp = self.request("GET", path, **kw)
        return resp.status_code, _safe_json(resp)

    def post_json(
        self, path: str, json_body: dict[str, Any], **kw: Any
    ) -> tuple[int, Any]:
        """POST a path and return ``(status, parsed_json_or_text)``. Never raises on 4xx."""
        resp = self.request("POST", path, json_body=json_body, **kw)
        return resp.status_code, _safe_json(resp)

    # ------------------------------------------------------------------- chat

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        stop: list[str] | str | None = None,
        seed: int | None = None,
        extra: dict[str, Any] | None = None,
        path: str = "/v1/chat/completions",
    ) -> Completion:
        """Non-streaming completion. Raises :class:`RelayError` on HTTP error."""
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if top_p is not None:
            body["top_p"] = top_p
        if stop is not None:
            body["stop"] = stop
        if seed is not None:
            body["seed"] = seed
        if extra:
            body.update(extra)

        t0 = time.monotonic()
        resp = self.request("POST", path, json_body=body, billable=True)
        latency = time.monotonic() - t0

        if resp.status_code != 200:
            raise RelayError(
                f"chat/completions returned {resp.status_code}",
                status=resp.status_code,
                body=resp.text[:2000],
            )

        payload = _safe_json(resp)
        if not isinstance(payload, dict):
            raise RelayError("chat/completions returned non-JSON body", status=resp.status_code)

        return _parse_completion(payload, model, latency)

    def chat_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        include_usage: bool = True,
        extra: dict[str, Any] | None = None,
        path: str = "/v1/chat/completions",
    ) -> StreamResult:
        """Streaming completion, with per-chunk arrival timing."""
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if include_usage:
            body["stream_options"] = {"include_usage": True}
        if extra:
            body.update(extra)

        t0 = time.monotonic()
        resp = self.request("POST", path, json_body=body, stream=True, billable=True)

        if resp.status_code != 200:
            body_text = resp.text[:2000]
            resp.close()
            raise RelayError(
                f"stream returned {resp.status_code}", status=resp.status_code, body=body_text
            )

        chunks: list[StreamChunk] = []
        text_parts: list[str] = []
        model_returned: str | None = None
        finish_reason: str | None = None
        usage_payload: dict[str, Any] = {}
        ttft: float | None = None

        for raw_line in resp.iter_lines(decode_unicode=True):
            if raw_line is None:
                continue
            line = raw_line.strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                piece = json.loads(data)
            except json.JSONDecodeError:
                continue

            t_offset = time.monotonic() - t0
            if piece.get("model"):
                model_returned = piece["model"]

            if isinstance(piece.get("usage"), dict):
                usage_payload = piece["usage"]

            delta_text = ""
            for choice in piece.get("choices") or []:
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    delta_text += content

            if delta_text:
                if ttft is None:
                    ttft = t_offset
                text_parts.append(delta_text)

            chunks.append(StreamChunk(index=len(chunks), t_offset_s=t_offset, text=delta_text, raw=piece))

        resp.close()
        usage = Usage.from_payload(usage_payload)

        return StreamResult(
            model_requested=model,
            model_returned=model_returned,
            text="".join(text_parts),
            chunks=chunks,
            usage=usage,
            finish_reason=finish_reason,
            ttft_s=ttft,
            total_s=time.monotonic() - t0,
        )

    # ----------------------------------------------------------------- models

    def list_models(self) -> list[str]:
        """``GET /v1/models``. Returns an empty list if unsupported."""
        status, payload = self.get_json("/v1/models")
        if status != 200 or not isinstance(payload, dict):
            return []
        out: list[str] = []
        for item in payload.get("data") or []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                out.append(item["id"])
        return out


# --------------------------------------------------------------------- helpers


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"_raw_text": resp.text[:4000]}


def _retry_after_seconds(resp: requests.Response) -> float:
    raw = resp.headers.get("Retry-After")
    if raw:
        try:
            return max(0.5, min(float(raw), 30.0))
        except ValueError:
            pass
    return 2.0


def _parse_completion(payload: dict[str, Any], requested: str, latency: float) -> Completion:
    content_parts: list[str] = []
    finish_reason: str | None = None

    for choice in payload.get("choices") or []:
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
        msg = choice.get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            content_parts.append(c)
        elif isinstance(c, list):
            # Some relays return the Anthropic-style content-block array.
            for block in c:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    content_parts.append(block["text"])

    return Completion(
        model_requested=requested,
        model_returned=payload.get("model"),
        content="".join(content_parts),
        usage=Usage.from_payload(payload.get("usage")),
        finish_reason=finish_reason,
        latency_s=latency,
        id=payload.get("id"),
        created=payload.get("created"),
        raw=payload,
    )


def iter_sse(resp: requests.Response) -> Iterator[dict[str, Any]]:
    """Yield parsed SSE data objects from an already-open streaming response."""
    for raw_line in resp.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue
