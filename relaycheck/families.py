"""Which vendor does a model name *claim* to belong to?

This lives outside ``probes/`` because three different consumers need it and
they must agree on the answer:

* ``selection.py`` picks a model list that spans vendors, otherwise the
  twin-detection probes compare aliases of a single backend and find nothing;
* ``probes/tokenizer.py`` decides whether a shared tokenizer is damning
  (different claimed vendors) or expected (same vendor, different tiers);
* ``probes/identity.py`` compares a model's self-description against the name
  it is sold under.

Keeping one implementation here means a name like ``gpt-4o-mini`` resolves the
same way everywhere.

Scope warning: this is a *name* classifier, not a truth classifier. It answers
"what does this name advertise", not "what is actually running". A relay can
name a model anything it likes.
"""

from __future__ import annotations

#: Known model families and the substrings their own models use when describing
#: themselves. Deliberately generous: we want low false negatives.
FAMILY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "openai": ("openai", "gpt-4", "gpt-3", "chatgpt"),
    "anthropic": ("anthropic", "claude"),
    "google": ("google", "gemini", "bard", "deepmind"),
    "deepseek": ("deepseek", "深度求索"),
    "moonshot": ("moonshot", "kimi", "月之暗面"),
    "minimax": ("minimax", "海螺"),
    "alibaba": ("alibaba", "qwen", "通义", "千问"),
    "zhipu": ("zhipu", "glm", "chatglm", "智谱", "清言"),
    "baidu": ("baidu", "ernie", "文心"),
    "bytedance": ("bytedance", "doubao", "豆包", "seed"),
    "meta": ("meta", "llama"),
    "mistral": ("mistral", "mixtral"),
    "xai": ("xai", "grok"),
    "01ai": ("01.ai", "yi-", "零一万物"),
}


def detect_family(text: str) -> set[str]:
    """Return every known family mentioned in ``text``."""
    low = text.lower()
    hits: set[str] = set()
    for family, keywords in FAMILY_KEYWORDS.items():
        if any(kw in low for kw in keywords):
            hits.add(family)
    return hits


def claimed_family(model_name: str) -> str | None:
    """Best-effort guess of the family a *model name* claims to belong to.

    A name can mention more than one vendor without being ambiguous about who
    sells it: ``deepseek-r1-distill-qwen-32b`` is a DeepSeek model distilled
    onto a Qwen base and relays sell it under exactly that string. Resolving it
    by sorted order picks ``alibaba``, and then a response whose ``model`` field
    says ``deepseek-r1`` looks like a cross-vendor swap — a MEDIUM accusation
    against a relay that did nothing wrong.

    The longest matching keyword wins instead: the vendor whose own name
    occupies the most of the string is the one the name is advertising. Genuine
    ties (``openai/claude-3-5-sonnet``, where both keywords are the same length)
    still break by sorted order, so two runs of one audit classify a name
    identically and two reports stay comparable.
    """
    low = model_name.lower()
    matches: list[tuple[int, str]] = []
    for family, keywords in FAMILY_KEYWORDS.items():
        longest = max((len(kw) for kw in keywords if kw in low), default=0)
        if longest:
            matches.append((longest, family))
    if not matches:
        return None
    best = max(length for length, _ in matches)
    return sorted(family for length, family in matches if length == best)[0]
