"""The pure relevance ranking Search sorts by before its deterministic order.

Ranking is deliberately narrow: both sides are reduced to whole tokens, a
query is a sequence of tokens and a title either matches that sequence or it
does not, so the rank is one number and nothing else. A token is a maximal
run of Unicode alphanumerics in the casefolded text; every other character is
a boundary between tokens, never part of one, so "Ring" is found in "The
Ring" but never inside "Daring".

The tiers, lower being better:

* 0 - the title's tokens equal the query's.
* 1 - the title's tokens start with the query's.
* 2 - the query's tokens appear contiguously somewhere in the title.
* 3 - every query token is present, one copy per copy in the title, anywhere.
* 4 - otherwise: no match at all.

Tier 4 is also the neutral tier. It is what an empty query, an empty title or
a query the caller never supplied ranks as, so an absent relevance can never
reorder what Search already decided.

Everything here is a pure function of two strings: no sources, no registry,
no configuration, no model, no clock and no Qt, and this module's imports are
kept to the standard library to make that structurally true.
"""
from __future__ import annotations

from collections import Counter

# A token is a maximal run of Unicode alphanumerics in the casefolded text.
# Everything else - whitespace, punctuation, symbols, anything a character
# class could call a boundary - separates runs and belongs to neither side.
_BOUNDARY_TEST = str.isalnum


def tokenize_relevance_text(text: str) -> tuple[str, ...]:
    """`text` casefolded and split into whole-token runs.

    Numeric runs, short tokens and repeated tokens are all preserved as-is:
    ranking never drops information, because dropping a token is exactly the
    kind of silent leniency a deterministic rank must not invent.
    """
    tokens: list[str] = []
    current: list[str] = []
    for char in text.casefold():
        if char.isalnum():
            current.append(char)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


def relevance_key(query_tokens: tuple[str, ...], title: str) -> tuple[int]:
    """The one relevance number a title ranks under a query.

    The result is a single-value tuple so it composes directly into the
    total sort key the search already builds: relevance sorts first, and
    everything else breaks the ties below it.
    """
    return (_tier_for(query_tokens, tokenize_relevance_text(title)),)


def _tier_for(query_tokens: tuple[str, ...], title_tokens: tuple[str, ...]) -> int:
    """Rank `title_tokens` under `query_tokens`, from exact to unrelated."""
    if not query_tokens or not title_tokens:
        # Nothing to match against, or nothing to match with: neutral. A
        # missing query must never reorder a result list, and an empty title
        # is no more a match than an empty query is.
        return 4

    if query_tokens == title_tokens:
        return 0

    width = len(query_tokens)
    if title_tokens[:width] == query_tokens:
        return 1

    if any(
        title_tokens[start : start + width] == query_tokens
        for start in range(len(title_tokens) - width + 1)
    ):
        return 2

    # The whole tokens are all there, just scattered. Consumption is a
    # multiset: two copies in the query need two copies in the title.
    needed = Counter(query_tokens)
    available = Counter(title_tokens)
    if all(available[token] >= needed[token] for token in needed):
        return 3

    return 4