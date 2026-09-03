"""Query building.

Deterministic normalisation plus the alias expansions configured in
config/settings.yaml. No LLM: a query rewrite that cannot be reproduced from the
config file cannot be explained on a trace.

The expansion is a recall aid for the lexical and embedding branches. It is not
applied to the reranker, which reads the customer's own words — see
`services.retrieve.service`.
"""

import re

from taskflow.config.settings import retrieval_config


def build_query(text: str) -> str:
    """Return the normalised query, with any configured aliases appended."""
    if not text or not text.strip():
        return ""

    normalized = text.strip().lower()
    expansions = retrieval_config().get("alias_expansions", {})

    matched = [
        str(expansion)
        for alias, expansion in expansions.items()
        if re.search(rf"\b{re.escape(alias)}\b", normalized, re.IGNORECASE)
    ]

    return f"{normalized} {' '.join(matched)}" if matched else normalized
