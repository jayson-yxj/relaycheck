"""Model selection.

Twin detection only works if the models we compare *claim* to be different
things. A relay that advertises 30 model names is usually 6 real backends with
5 aliases each, so picking the first N names in list order is the worst possible
strategy — it can hand us five aliases of one backend and no comparison at all.

So we pick for **family diversity first**: one representative per claimed
family, then fill any remaining slots. That maximises the chance of catching a
relay that forwards an expensive model name to a cheap backend.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .families import claimed_family

#: Substrings that mark a model as not a chat completion target.
_NON_CHAT_MARKERS = (
    "embed",
    "embedding",
    "whisper",
    "tts",
    "stt",
    "rerank",
    "moderation",
    "dall-e",
    "dalle",
    "stable-diffusion",
    "flux",
    "sora",
    "image",
    "audio",
    "speech",
    "voice",
    "video",
    "ocr",
    "bge-",
    "text-similarity",
)


def is_chat_model(name: str) -> bool:
    low = name.lower()
    return not any(marker in low for marker in _NON_CHAT_MARKERS)


def select_models(
    available: Sequence[str],
    limit: int,
    explicit: Sequence[str] | None = None,
) -> list[str]:
    """Choose up to ``limit`` model names to audit."""
    if explicit:
        picked = [m for m in explicit if m]
        return picked[:limit] if limit > 0 else picked

    candidates = [m for m in available if is_chat_model(m)]
    if not candidates:
        candidates = list(available)

    # Bucket by claimed family, preserving first-seen order within each bucket.
    buckets: dict[str, list[str]] = {}
    order: list[str] = []
    for name in candidates:
        family = claimed_family(name) or f"~other:{name.lower()}"
        if family not in buckets:
            buckets[family] = []
            order.append(family)
        buckets[family].append(name)

    picked: list[str] = []
    # Round-robin across families so we get maximum diversity early.
    depth = 0
    while len(picked) < limit:
        added = False
        for family in order:
            bucket = buckets[family]
            if depth < len(bucket):
                picked.append(bucket[depth])
                added = True
                if len(picked) >= limit:
                    break
        if not added:
            break
        depth += 1

    return picked[:limit]


def describe_selection(picked: Iterable[str]) -> str:
    return ", ".join(f"{m}[{claimed_family(m) or '?'}]" for m in picked)
