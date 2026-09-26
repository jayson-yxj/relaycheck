"""relaycheck — detect model substitution and billing fraud in LLM API relays.

The tool answers three questions a relay user cannot otherwise answer:

1. Is the model I am calling actually the model I asked for?
2. Am I paying for tokens I cannot see?
3. Is the billing basis what the merchant says it is?

Everything is client-side, read-only, and evidence-based: every finding carries
the raw response fragments that produced it.
"""

__version__ = "0.1.3"
__all__ = ["__version__"]
